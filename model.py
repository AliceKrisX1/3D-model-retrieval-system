# coding=utf-8
import tensorflow as tf
import re
import numpy as np
import globals as g_

FLAGS = tf.app.flags.FLAGS
# Basic model parameters.
tf.app.flags.DEFINE_integer('batch_size', g_.BATCH_SIZE,
                            """Number of images to process in a batch.""")
tf.app.flags.DEFINE_float('learning_rate', g_.INIT_LEARNING_RATE,
                          """Initial learning rate.""")

# Constants describing the training process.
MOVING_AVERAGE_DECAY = 0.9999  # The decay to use for the moving average.
NUM_EPOCHS_PER_DECAY = 350.0  # Epochs after which learning rate decays.
LEARNING_RATE_DECAY_FACTOR = 0.1  # Learning rate decay factor.
WEIGHT_DECAY_FACTOR = 0.004 / 5.  # 3500 -> 2.8

TOWER_NAME = 'tower'
DEFAULT_PADDING = 'SAME'


def _activation_summary(x):
    """Helper to create summaries for activations.
    Creates a summary that provides a histogram of activations.
    Creates a summary that measure the sparsity of activations.
    Args:
      x: Tensor
    Returns:
      nothing
    """
    # Remove 'tower_[0-9]/' from the name in case this is a multi-GPU training
    # session. This helps the clarity of presentation on tensorboard.
    tensor_name = re.sub('%s_[0-9]*/' % TOWER_NAME, '', x.op.name)
    tf.compat.v1.summary.histogram(tensor_name + '/activations', x)
    tf.compat.v1.summary.scalar(tensor_name + '/sparsity', tf.nn.zero_fraction(x))


def _variable_on_cpu(name, shape, initializer):
    """Helper to create a Variable stored on CPU memory.
    Args:
      name: name of the variable
      shape: list of ints
      initializer: initializer for Variable
    Returns:
      Variable Tensor
    """
    with tf.device('/cpu:0'):
        var = tf.compat.v1.get_variable(name, shape, initializer=initializer)
    return var


def _variable_with_weight_decay(name, shape, wd):
    """Helper to create an initialized Variable with weight decay.
    Note that the Variable is initialized with a truncated normal distribution.
    A weight decay is added only if one is specified.
    Args:
      name: name of the variable
      shape: list of ints
      wd: add L2Loss weight decay multiplied by this float. If None, weight
          decay is not added for this Variable.
    Returns:
      Variable Tensor
    """
    var = _variable_on_cpu(name, shape,
                           initializer=tf.contrib.layers.xavier_initializer())
    if wd:
        weight_decay = tf.multiply(tf.nn.l2_loss(var), wd, name='weight_loss')
        tf.compat.v1.add_to_collection('losses', weight_decay)
    return var


def _conv(name, in_, ksize, strides=[1, 1, 1, 1], padding=DEFAULT_PADDING, group=1, reuse=False):
    n_kern = ksize[3]  # 卷积核个数
    convolve = lambda i, k: tf.nn.conv2d(i, k, strides, padding=padding)

    with tf.compat.v1.variable_scope(name, reuse=reuse) as scope:
        if group == 1:
            kernel = _variable_with_weight_decay('weights', shape=ksize, wd=0.0)
            conv = convolve(in_, kernel)
        else:
            ksize[2] /= group
            kernel = _variable_with_weight_decay('weights', shape=ksize, wd=0.0)
        input_groups = tf.split(in_, group, 3)
        kernel_groups = tf.split(kernel, group, 3)
        output_groups = [convolve(i, k) for i, k in zip(input_groups, kernel_groups)]
        # Concatenate the groups
        conv = tf.concat(output_groups, 3)

        biases = _variable_on_cpu('biases', [n_kern], tf.constant_initializer(0.0))
        conv = tf.nn.bias_add(conv, biases)
        conv = tf.nn.relu(conv, name=scope.name)
        _activation_summary(conv)

    # print(name, conv.get_shape().as_list())          # 写报告的时候输出
    return conv


def _maxpool(name, in_, ksize, strides, padding=DEFAULT_PADDING):
    pool = tf.nn.max_pool2d(in_, ksize=ksize, strides=strides,
                            padding=padding, name=name)

    # print(name, pool.get_shape().as_list())          # 写报告的时候输出
    return pool


def _fc(name, in_, outsize, dropout=1.0, reuse=False):
    with tf.compat.v1.variable_scope(name, reuse=reuse) as scope:
        # Move everything into depth so we can perform a single matrix multiply.

        insize = in_.get_shape().as_list()[-1]
        weights = _variable_with_weight_decay('weights', shape=[insize, outsize], wd=0.004)
        biases = _variable_on_cpu('biases', [outsize], tf.constant_initializer(0.0))
        fc = tf.nn.relu(tf.matmul(in_, weights) + biases, name=scope.name)
        fc = tf.nn.dropout(fc, dropout)

        _activation_summary(fc)

    # print(name, fc.get_shape().as_list())          # 写报告的时候输出
    return fc


def inference_multiview(views, n_classes, keep_prob, retrieval=False):
    """
    views: N x V x W x H x C tensor
    retrieval: 表示是否做检索操作，true则返回fc7，false返回fc8
    """
    n_views = views.get_shape().as_list()[1]  # 12或1
    # transpose views : (NxVxWxHxC) -> (VxNxWxHxC)
    views = tf.transpose(views, perm=[1, 0, 2, 3, 4])
    view_pool = []
    for i in range(n_views):  # 对每个模型的n_views张图
        # set reuse True for i > 0, for weight-sharing
        reuse = (i != 0)
        view = tf.gather(views, i)  # 提取出某个模型的第i张图片 格式： NxWxHxC

        conv1 = _conv('conv1', view, [11, 11, 3, 96], [1, 4, 4, 1], 'VALID', reuse=reuse)
        lrn1 = None
        pool1 = _maxpool('pool1', conv1, ksize=[1, 3, 3, 1], strides=[1, 2, 2, 1], padding='VALID')

        conv2 = _conv('conv2', pool1, [5, 5, 96, 256], group=2, reuse=reuse)  # SAME
        lrn2 = None
        pool2 = _maxpool('pool2', conv2, ksize=[1, 3, 3, 1], strides=[1, 2, 2, 1], padding='VALID')

        conv3 = _conv('conv3', pool2, [3, 3, 256, 384], reuse=reuse)  # SAME
        conv4 = _conv('conv4', conv3, [3, 3, 384, 384], group=2, reuse=reuse)  # SAME
        conv5 = _conv('conv5', conv4, [3, 3, 384, 256], group=2, reuse=reuse)  # SAME

        pool5 = _maxpool('pool5', conv5, ksize=[1, 3, 3, 1], strides=[1, 2, 2, 1], padding='VALID')

        dim = np.prod(pool5.get_shape().as_list()[1:])
        reshape = tf.reshape(pool5, [-1, dim])

        view_pool.append(reshape)

    pool5_vp = _view_pool(view_pool, 'pool5_vp')
    fc6 = _fc('fc6', pool5_vp, 4096, dropout=keep_prob)
    fc7 = _fc('fc7', fc6, 4096, dropout=keep_prob)
    fc8 = _fc('fc8', fc7, n_classes)
    if retrieval:
        return fc7
    else:
        return fc8


def load_alexnet_to_mvcnn(sess, caffetf_modelpath):
    """ caffemodel: np.array, """

    caffemodel = np.load(caffetf_modelpath, encoding='latin1',
                         allow_pickle=True)  #############################################
    data_dict = caffemodel.item()

    for l in ['conv1', 'conv2', 'conv3', 'conv4', 'conv5', 'fc6', 'fc7']:
        name = l
        _load_param(sess, name, data_dict[l])


def _load_param(sess, name, layer_data):
    w, b = layer_data

    with tf.compat.v1.variable_scope(name, reuse=True):
        for subkey, data in zip(('weights', 'biases'), (w, b)):
            print('loading ', name, subkey)

            try:
                var = tf.compat.v1.get_variable(subkey)
                sess.run(var.assign(data))
            except ValueError as e:
                print('varirable loading failed:', subkey, '(%s)' % str(e))


def _view_pool(view_features, name):
    vp = tf.expand_dims(view_features[0], 0)  # eg. [100] -> [1, 100]
    for v in view_features[1:]:
        v = tf.expand_dims(v, 0)
        vp = tf.concat([vp, v], 0)
    # print('vp before reducing:', vp.get_shape().as_list())
    vp = tf.reduce_max(vp, [0], name=name)
    # print('vp after reducing:', vp.get_shape().as_list())
    return vp


def loss(fc8, labels):
    l = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=labels, logits=fc8)
    l = tf.reduce_mean(l)

    tf.compat.v1.add_to_collection('losses', l)

    return tf.add_n(tf.compat.v1.get_collection('losses'), name='total_loss')


def classify(fc8):
    softmax = tf.nn.softmax(fc8)
    y = tf.argmax(softmax, 1)  # 返回列表中最大索引
    return y


def _add_loss_summaries(total_loss):
    """Add summaries for losses in CIFAR-10 model.
    Generates moving average for all losses and associated summaries for
    visualizing the performance of the network.
    Args:
    total_loss: Total loss from loss().
    Returns:
    loss_averages_op: op for generating moving averages of losses.
    """
    # Compute the moving average of all individual losses and the total loss.
    loss_averages = tf.train.ExponentialMovingAverage(0.9, name='avg')
    losses = tf.compat.v1.get_collection('losses')
    print('losses:', losses)
    loss_averages_op = loss_averages.apply(losses + [total_loss])
    # Attach a scalar summary to all individual losses and the total loss; do the
    # same for the averaged version of the losses.
    for l in losses + [total_loss]:
        # Name each loss as '(raw)' and name the moving average version of the loss
        # as the original loss name.
        tf.compat.v1.summary.scalar(l.op.name + ' (raw)', l)
        tf.compat.v1.summary.scalar(l.op.name, loss_averages.average(l))
    return loss_averages_op


def train(total_loss, global_step, data_size):
    num_batches_per_epoch = data_size / FLAGS.batch_size
    decay_steps = int(num_batches_per_epoch * NUM_EPOCHS_PER_DECAY)

    lr = tf.compat.v1.train.exponential_decay(FLAGS.learning_rate,
                                              global_step,
                                              decay_steps,
                                              LEARNING_RATE_DECAY_FACTOR,
                                              staircase=True)
    tf.compat.v1.summary.scalar('learning_rate', lr)

    loss_averages_op = _add_loss_summaries(total_loss)

    with tf.control_dependencies([loss_averages_op]):
        opt = tf.compat.v1.train.AdamOptimizer(lr)
        grads = opt.compute_gradients(total_loss)

    # apply gradients
    apply_gradient_op = opt.apply_gradients(grads, global_step=global_step)

    for var in tf.compat.v1.trainable_variables():
        tf.compat.v1.summary.histogram(var.op.name, var)

    for grad, var in grads:
        if grad is not None:
            tf.compat.v1.summary.histogram(var.op.name + '/gradients', grad)

    variable_averages = tf.train.ExponentialMovingAverage(
        MOVING_AVERAGE_DECAY, global_step)

    variable_averages_op = variable_averages.apply(tf.compat.v1.trainable_variables())

    with tf.control_dependencies([apply_gradient_op, variable_averages_op]):
        train_op = tf.no_op(name='train')

    return train_op
