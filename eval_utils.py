# Parts computing square root of a pd matrix and computing FID scores taken from 
# https://github.com/tensorflow/tensorflow/blob/master/tensorflow/contrib/gan/python/eval/python/classifier_metrics_impl.py
# Parts manipulating Inception network taken from 
# https://github.com/bioinf-jku/TTUR

import tensorflow as tf
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import linalg_ops
import logging
import os
import sys
import numpy as np
from datahandler import DataHandler
import scipy.linalg as la

CELEBA_DIR ='../celebA/datasets/celeba/img_align_celeba'
CIFAR10_DIR = '../cifar10'
DSPRITES_DIR = '../dsprites'
GRASSLI_DIR = '../grassli'
MNIST_DIR = '../mnist'
INCEPTION_PATH = '../FID-scores/classify_image_graph_def.pb'

def compute_metrics(tup, num_samples, work_dir, batch_size, recompute):
    # 1. Finding out details of the trained model
    exp_path, model_path, model_filename = tup
    param_file = os.path.join(exp_path, 'params.txt')
    if not os.path.exists(param_file):
        logging.error(' -- Directory %s does not contain params.txt' % exp_path)
        return None
    opts = model_details(param_file, work_dir)
    # logging.error(opts)
    # 2. Loading pre-computed dataset FID stats
    stats = np.load(os.path.join(
        opts['data_dir'],
        opts['dataset'] + '.fidstats' + str(num_samples) + '.npz'))
    data_mu, data_cov = stats['mu'], stats['cov']
    # 3. Generating model samples and auto-encoding training samples
    samples_path = os.path.join(
        model_path, model_filename + '.samples' + str(num_samples) + '.npy')
    reconstr_path = os.path.join(
        model_path, model_filename + '.reconstr' + str(num_samples) + '.npy')
    if os.path.exists(samples_path) and os.path.exists(reconstr_path) and recompute == 'no':
        logging.error(' -- Samples and training reconstruction already available')
        gen = np.load(samples_path)
        train_reconstr = np.load(reconstr_path)
    elif os.path.exists(samples_path) and not os.path.exists(reconstr_path) and recompute == 'no':
        logging.error(' -- Samples already available but reconstructions are not. Evaluating only samples')
        gen = np.load(samples_path)
        train_reconstr = None
    else:
        logging.error(' -- Generating samples and training reconstructions')
        data = DataHandler(opts)
        random_ids = np.random.choice(data.num_points,
                                      num_samples, replace=False)
        to_autoencode = data.data[random_ids]
        gen, train_reconstr = run_model(
            model_path, model_filename, num_samples, opts, to_autoencode, batch_size)
        if type(gen) is not np.ndarray:
            return None
        if opts['input_normalize_sym']:
            gen = gen / 2. + 0.5
            train_reconstr = train_reconstr / 2. + 0.5
        sample_sharp = compute_blurriness(gen[:min(500, num_samples)])
        reconstr_sharp = compute_blurriness(
            train_reconstr[:min(500, num_samples)])
        logging.error(' -- Samples sharpness = %f' % np.mean(sample_sharp))
        logging.error(' -- Reconstruction sharpness = %f' % np.mean(reconstr_sharp))
        np.save(os.path.join(model_path, model_filename + '.samples' + \
                str(num_samples) + '.npy'), gen)
        np.save(os.path.join(model_path, model_filename + '.reconstr' + \
                str(num_samples) + '.npy'), train_reconstr)
        np.savez(os.path.join(model_path, model_filename + '.sharp' + \
                 str(num_samples)),
                 sharp_gen=sample_sharp,
                 sharp_reconstr=reconstr_sharp)
    # Just in case checking if pictures are properly normalized
    if not np.all(gen >= 0):
        gen = (gen + 1.) / 2.
    if train_reconstr is not None and not np.all(train_reconstr >= 0):
        train_reconstr = (train_reconstr + 1.) / 2.
    # 4. Computing FID of generated samples
    logging.error(' -- Computing FID of generated samples')
    fid_gen = fid_using_samples((data_mu, data_cov), gen, batch_size)
    if train_reconstr is not None:
        logging.error(' -- Computing FID of reconstructed training images')
        fid_reconstr = fid_using_samples((data_mu, data_cov),
                                         train_reconstr, batch_size)
    else:
        fid_reconstr = None

    if type(fid_gen) != tuple:
        return None

    np.savez(os.path.join(model_path, model_filename + '.fidstats' + \
            str(num_samples)),
            mu_gen=fid_gen[1],
            cov_gen=fid_gen[2],
            mu_rec=None if fid_reconstr is None else fid_reconstr[1],
            cov_rec=None if fid_reconstr is None else fid_reconstr[2])

    return fid_gen[0], None if fid_reconstr is None else fid_reconstr[0]

def model_details(param_filename, work_dir):
    with open(param_filename, 'r') as f:
        lines = f.readlines()
    for line in lines:
        if 'zdim :' in line:
            zdim = int(line.split(' : ')[-1])
        if 'dataset :' in line:
            dataset = line.split(' : ')[-1][:-1]
        if 'pz_scale :' in line:
            pz_scale = float(line.split(' : ')[-1])
        if 'pz :' in line:
            pz = line.split(' : ')[-1][:-1]
        if 'input_normalize_sym :' in line:
            normalize = ('True' in line)
    opts = {}
    opts['dataset'] = dataset
    opts['input_normalize_sym'] = normalize
    opts['work_dir'] = work_dir
    opts['celebA_crop'] = 'closecrop'
    opts['pz'] = pz
    opts['zdim'] = zdim
    opts['pz_scale'] = pz_scale
    if dataset == 'celebA':
        opts['data_dir'] = CELEBA_DIR
    elif dataset == 'cifar10':
        opts['data_dir'] = CIFAR10_DIR
    elif dataset == 'dsprites':
        opts['data_dir'] = DSPRITES_DIR
    elif dataset == 'grassli':
        opts['data_dir'] = GRASSLI_DIR
    elif dataset == 'mnist':
        opts['data_dir'] = MNIST_DIR
    return opts

def run_model(path, filename, num_samples, opts, to_reconstr, batch_size):
    with tf.Session() as sess:
        with sess.graph.as_default():
            # try:
            saver = tf.train.import_meta_graph(
                os.path.join(path, filename + '.meta'))
            saver.restore(sess, os.path.join(path, filename))
            real_points_ph = tf.get_collection('real_points_ph')[0]
            noise_ph = tf.get_collection('noise_ph')[0]
            is_training_ph = tf.get_collection('is_training_ph')[0]
            decoder = tf.get_collection('decoder')[0]
            encoder = tf.get_collection('encoder')
            if len(encoder) > 0:
                encoder = encoder[0]
                encoder_log_sigma = None
            else:
                # VAE model
                encoder = tf.get_collection('encoder_mean')[0]
                encoder_log_sigma = tf.get_collection('encoder_log_sigma')[0]
            # except:
            #     logging.error(" -- Unexpected error:", sys.exc_info()[0])
            #     return None
            zdim = opts['zdim']
            pz = opts['pz']
            pz_scale = opts['pz_scale']

            # 1. Generating random samples
            if pz == 'uniform':
                noise = np.random.uniform(-1, 1, [num_samples, zdim])
            elif pz in ('normal', 'sphere'):
                noise = np.random.randn(num_samples, zdim)
                if pz == 'sphere':
                    noise = noise / np.sqrt(
                        np.sum(noise * noise, axis=1))[:, np.newaxis]
            z = pz_scale * noise
            z = np.reshape(z, [-1, zdim])
            gen = []
            for ibatch in xrange(num_samples / batch_size):
                batch = z[ibatch * batch_size : (ibatch + 1) * batch_size]
                try:
                    gen_batch = sess.run(
                        decoder,
                        feed_dict={noise_ph: batch, is_training_ph: False})
                except:
                    logging.error(" -- Unexpected error:", sys.exc_info()[0])
                    return None
                gen.append(gen_batch)
            gen = np.vstack(gen)

            # 2. Auto-encoding training pictures
            reconstr = []
            for ibatch in xrange(num_samples / batch_size):
                batch = to_reconstr[ibatch * batch_size : (ibatch + 1) * batch_size]
                # try:
                encoded_batch = sess.run(
                    encoder, feed_dict={real_points_ph: batch,
                                        is_training_ph: False})
                if encoder_log_sigma != None:
                    # We have VAE, need to add the scaled noise
                    batch_log_sigma = sess.run(
                        encoder_log_sigma, feed_dict={real_points_ph: batch,
                                                      is_training_ph: False})
                    noise = z[ibatch * batch_size : (ibatch + 1) * batch_size]
                    scaled_noise = np.multiply(np.exp(batch_log_sigma / 2.), noise)
                    encoded_batch += scaled_noise

                reconstructed_batch = sess.run(
                    decoder, feed_dict={noise_ph: encoded_batch,
                                        is_training_ph: False})
                # except:
                #     logging.error(" -- Unexpected error:", sys.exc_info()[0])
                #     return None
                reconstr.append(reconstructed_batch)
            reconstr = np.vstack(reconstr)

    tf.reset_default_graph()
    return gen, reconstr

# code for handling inception net derived from
#   https://github.com/openai/improved-gan/blob/master/inception_score/model.py
def _get_inception_layer(sess):
    """Prepares inception net for batched usage and returns pool_3 layer. """
    layername = 'FID_Inception_Net/pool_3:0'
    pool3 = sess.graph.get_tensor_by_name(layername)
    ops = pool3.graph.get_operations()
    for op_idx, op in enumerate(ops):
        for o in op.outputs:
            shape = o.get_shape()
            if shape._dims is not None:
              shape = [s.value for s in shape]
              new_shape = []
              for j, s in enumerate(shape):
                if s == 1 and j == 0:
                  new_shape.append(None)
                else:
                  new_shape.append(s)
              o._shape = tf.TensorShape(new_shape)
    return pool3

def compute_inception_stats(points, batch_size, _sess=None):
    if _sess:
        sess = _sess
    else:
        sess = tf.Session()
    # Safe to assume len(points) > batch_size and divisible bybatch_size 
    path = INCEPTION_PATH
    with tf.gfile.FastGFile(path, 'rb') as f:
        graph_def = tf.GraphDef()
        graph_def.ParseFromString(f.read())
        _ = tf.import_graph_def(graph_def, name='FID_Inception_Net')

    sess.run(tf.global_variables_initializer())
    inception_op = _get_inception_layer(sess)
    res = []
    for ibatch in range(len(points) / batch_size):
        # logging.error(' -- Propagating batch %d / %d' % (ibatch + 1, len(points) / batch_size))
        batch = 256 * points[ibatch * batch_size:(ibatch + 1) * batch_size]
        batch_prop = sess.run(
            inception_op, feed_dict={'FID_Inception_Net/ExpandDims:0': batch})
        res.append(batch_prop.reshape(batch_size, -1))
    res = np.vstack(res)

    mu = np.mean(res, axis=0)
    cov = np.cov(res, rowvar=False)

    if not _sess:
        sess.close()
        tf.reset_default_graph()

    return mu, cov

# https://github.com/tensorflow/tensorflow/blob/master/tensorflow/contrib/gan/python/eval/python/classifier_metrics_impl.py
def sym_matrix_sqrt(mat):
    sval, umat, vmat = linalg_ops.svd(mat)
    si = array_ops.where(math_ops.less(sval, 1e-10),
                         sval, math_ops.sqrt(sval))
    return math_ops.matmul(math_ops.matmul(
        umat, array_ops.diag(si)), vmat, transpose_b=True)

# https://github.com/tensorflow/tensorflow/blob/master/tensorflow/contrib/gan/python/eval/python/classifier_metrics_impl.py
def trace_sqrt_product(cov1, cov2):
    sqrt_cov1 = sym_matrix_sqrt(cov1)
    temp = math_ops.matmul(
        sqrt_cov1, math_ops.matmul(cov2, sqrt_cov1))

    return math_ops.trace(sym_matrix_sqrt(temp))

def fid_using_samples(real, gen, batch_size):
    data_mu, data_cov = real

    with tf.Session() as sess:
        logging.error(' -- Computing Inception stats')
        gen_mu, gen_cov = compute_inception_stats(gen, batch_size, sess)
        logging.error(' -- Computing Frechet distance')
        m = np.sum(np.square(data_mu - gen_mu))
        s = trace_sqrt_product(data_cov, gen_cov)
        s = sess.run(s)
        trace_data = np.trace(data_cov)
        trace_gen = np.trace(gen_cov)
        dist = m + trace_data + trace_gen - 2.0 * s
        # logging.error('Mean=%f, trace_data=%f, trace_gen=%f, trace_sqrt=%f' %\
        #        (m, trace_data, trace_gen, s))

    if np.isnan(dist):
        logging.error(' -- NaNs appeared while computing FID')
        return None

    tf.reset_default_graph()
    return (dist, gen_mu, gen_cov)

def compute_blurriness(images):
    with tf.Session() as sess:
        with sess.graph.as_default():
            sample_size = images.shape[0]
            # First convert to greyscale
            if images.shape[-1] > 1:
                # We have RGB
                images = tf.image.rgb_to_grayscale(images)
            images = tf.cast(images, tf.float32)
            # Next convolve with the Laplace filter
            lap_filter = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]])
            lap_filter = lap_filter.reshape([3, 3, 1, 1]).astype(np.float32)
            conv = tf.nn.conv2d(images, lap_filter,
                                strides=[1, 1, 1, 1], padding='VALID')
            # And finally get the variance
            _, lapvar = tf.nn.moments(conv, axes=[1, 2, 3])
            # run the op
            blurr = sess.run(lapvar)
    tf.reset_default_graph()
    return blurr
