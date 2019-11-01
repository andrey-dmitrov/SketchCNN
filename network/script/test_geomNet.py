#
# Project SketchCNN
#
#   Author: Changjian Li (chjili2011@gmail.com),
#   Copyright (c) 2018. All Rights Reserved.
#
# ==============================================================================
"""Geometry regression network testing.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import os
import logging
import argparse

import tensorflow as tf
from network import SKETCHNET
from loader import SketchReader
from utils.util_func import slice_tensor, make_dir, dump_params
import cv2
import numpy as np

# Hyper Parameters
hyper_params = {
    'dbTest': '',
    'outDir': '',
    'device': '0',
    'rootFt': 32,
    'cktDir': '',
    'nbThreads': 1,
    'dsWeight': 5.0,
    'regWeight': 2.0,
    'dlossScale': 920.0,
    'nlossScale': 430.0,
    'graphName': '',
}

nprLine_input = tf.placeholder(tf.float32, [None, None, None, 1], name='npr_input')
ds_input = tf.placeholder(tf.float32, [None, None, None, 1], name='ds_input')
fm_input = tf.placeholder(tf.float32, [None, None, None, 1], name='fLMask_input')
fmInv_input = tf.placeholder(tf.float32, [None, None, None, 1], name='fLInvMask_input')
gtNormal_input = tf.placeholder(tf.float32, [None, None, None, 3], name='gtN_input')
gtDepth_input = tf.placeholder(tf.float32, [None, None, None, 1], name='gtD_input')
gtField_input = tf.placeholder(tf.float32, [None, None, None, 4], name='gtField_input')
clineInvMask_input = tf.placeholder(tf.float32, [None, None, None, 1], name='clIMask_input')
maskShape_input = tf.placeholder(tf.float32, [None, None, None, 1], name='shapeMask_input')
maskDs_input = tf.placeholder(tf.float32, [None, None, None, 1], name='dsMask_input')
mask2D_input = tf.placeholder(tf.float32, [None, None, None, 1], name='2dMask_input')
selLineMask_input = tf.placeholder(tf.float32, [None, None, None, 1], name='sLMask_input')
vdotnScalar_input = tf.placeholder(tf.float32, [None, None, None, 1], name='curvMag_input')


# depth, normal regularization term
def reg_loss(logit_n, logit_d, shape_mask, cl_mask_inverse, fl_mask_inv, scope='reg_loss'):
    with tf.name_scope(scope) as _:
        # convert normal signal back to [-1, 1]
        converted_n = (logit_n * 2.0) - 1.0

        img_shape = tf.shape(logit_d)
        N = img_shape[0]
        H = img_shape[1]
        W = img_shape[2]
        K = 0.007843137254902

        shape_mask_crop = slice_tensor(shape_mask, logit_d)
        l_mask_crop = slice_tensor(cl_mask_inverse, logit_d)
        fl_mask_inv_crop = slice_tensor(fl_mask_inv, logit_d)
        combined_mask = shape_mask_crop * l_mask_crop * fl_mask_inv_crop
        mask_shift_x = tf.slice(combined_mask, [0, 0, 0, 0], [-1, -1, W - 1, -1])
        mask_shift_y = tf.slice(combined_mask, [0, 0, 0, 0], [-1, H - 1, -1, -1])

        c0 = tf.fill([N, H, W - 1, 1], K)
        c1 = tf.zeros(shape=[N, H, W - 1, 1])

        cx = logit_d[:, :, 1:, :] - logit_d[:, :, :-1, :]
        t_x = tf.concat([c0, c1, cx], axis=3)
        # approximate normalization
        t_x /= K

        c2 = tf.zeros(shape=[N, H - 1, W, 1])
        c3 = tf.fill([N, H - 1, W, 1], K)
        cy = logit_d[:, 1:, :, :] - logit_d[:, :-1, :, :]
        t_y = tf.concat([c2, c3, cy], axis=3)
        # approximate normalization
        t_y /= K

        normal_shift_x = tf.slice(converted_n, [0, 0, 0, 0], [-1, -1, W - 1, -1])
        normal_shift_y = tf.slice(converted_n, [0, 0, 0, 0], [-1, H - 1, -1, -1])

        reg_loss1_diff = tf.reduce_sum(t_x * normal_shift_x, 3)
        reg_loss1 = tf.losses.mean_squared_error(tf.zeros(shape=[N, H, W - 1]), reg_loss1_diff,
                                                 weights=tf.squeeze(mask_shift_x, [3]))

        reg_loss2_diff = tf.reduce_sum(t_y * normal_shift_y, 3)
        reg_loss2 = tf.losses.mean_squared_error(tf.zeros(shape=[N, H - 1, W]), reg_loss2_diff,
                                                 weights=tf.squeeze(mask_shift_y, [3]))

        return reg_loss1 + reg_loss2


# total loss
def loss(logit_d, logit_n, logit_c, normal, depth, shape_mask, ds_mask, cl_mask_inverse,
         gt_ds, npr, logit_f, gt_f, fl_mask_inv):
    img_shape = tf.shape(logit_d)
    N = img_shape[0]
    H = img_shape[1]
    W = img_shape[2]

    mask_crop = slice_tensor(shape_mask, logit_n)
    mask_crop3 = tf.tile(mask_crop, [1, 1, 1, 3])

    zero_tensor = tf.zeros(shape=[N, H, W, 1])
    zero_tensor3 = tf.zeros(shape=[N, H, W, 3])
    logit_c3 = tf.tile(logit_c, [1, 1, 1, 3])

    # normal loss (l2)
    gt_normal = slice_tensor(normal, logit_n)
    n_loss = tf.losses.mean_squared_error(zero_tensor3, logit_c3 * (gt_normal - logit_n),
                                          weights=mask_crop3)

    real_n_loss = tf.losses.absolute_difference(gt_normal, logit_n, weights=mask_crop3)

    # depth loss (l2)
    gt_depth = slice_tensor(depth, logit_n)
    d_loss = tf.losses.mean_squared_error(zero_tensor, logit_c * (gt_depth - logit_d),
                                          weights=mask_crop)

    real_d_loss = tf.losses.absolute_difference(gt_depth, logit_d, weights=mask_crop)

    # omega_loss (l2)
    omega_loss = tf.losses.mean_squared_error(zero_tensor, logit_c - 1.0, weights=mask_crop)

    # depth sample loss (l2)
    d_mask_crop = slice_tensor(ds_mask, logit_n)
    ds_loss = tf.losses.mean_squared_error(gt_depth, logit_d, weights=d_mask_crop)

    # regularization loss (l2)
    r_loss = reg_loss(logit_n, logit_d, shape_mask, cl_mask_inverse, fl_mask_inv)

    total_loss = hyper_params['dlossScale'] * d_loss + hyper_params['nlossScale'] * n_loss + omega_loss + \
                 hyper_params['dsWeight'] * ds_loss + hyper_params['regWeight'] * r_loss

    shape_mask_crop = slice_tensor(shape_mask, logit_n)
    shape_mask_crop3 = tf.tile(shape_mask_crop, [1, 1, 1, 3])
    shape_mask_crop4 = tf.tile(shape_mask_crop, [1, 1, 1, 4])
    cl_mask_inverse4 = tf.tile(cl_mask_inverse, [1, 1, 1, 4])
    gt_f = slice_tensor(gt_f, logit_n) * cl_mask_inverse4
    logit_f = logit_f * shape_mask_crop4 * cl_mask_inverse4
    cur_shape = tf.shape(logit_n)
    lc = tf.zeros([cur_shape[0], cur_shape[1], cur_shape[2], 1], tf.float32)
    gt_coeff_a = tf.concat([tf.slice(gt_f, [0, 0, 0, 0], [-1, -1, -1, 2]), lc], axis=3)
    gt_coeff_b = tf.concat([tf.slice(gt_f, [0, 0, 0, 2], [-1, -1, -1, 2]), lc], axis=3)
    f_coeff_a = tf.concat([tf.slice(logit_f, [0, 0, 0, 0], [-1, -1, -1, 2]), lc], axis=3)
    f_coeff_b = tf.concat([tf.slice(logit_f, [0, 0, 0, 2], [-1, -1, -1, 2]), lc], axis=3)

    return total_loss, d_loss, n_loss, ds_loss, r_loss, real_d_loss, real_n_loss, omega_loss, \
           gt_normal * shape_mask_crop3, logit_n * shape_mask_crop3, gt_depth * shape_mask_crop, \
           logit_d * shape_mask_crop, gt_ds * shape_mask_crop, npr, \
           slice_tensor(cl_mask_inverse, logit_n) * mask_crop, \
           logit_c * shape_mask_crop, gt_coeff_a, gt_coeff_b, f_coeff_a, f_coeff_b


# testing process
def test_procedure(net, test_records):
    # Load data
    reader = SketchReader(tfrecord_list=test_records, raw_size=[256, 256, 25],
                          shuffle=False, num_threads=hyper_params['nbThreads'],
                          batch_size=1, nb_epoch=1)
    raw_input = reader.next_batch()

    npr_lines, ds, _, fm, fm_inv, gt_normal, gt_depth, gt_field, mask_cline_inv, mask_shape, mask_ds, _, _, \
    mask2d, selm, ndotv = net.cook_raw_inputs(raw_input)

    # Network forward
    logit_f, _ = net.load_field_net(nprLine_input,
                                    mask2D_input,
                                    ds_input,
                                    fm_input,
                                    selLineMask_input,
                                    vdotnScalar_input,
                                    hyper_params['rootFt'],
                                    is_training=False)

    logit_d, logit_n, logit_c, _ = net.load_GeomNet(nprLine_input,
                                                    ds_input,
                                                    mask2D_input,
                                                    fm_input,
                                                    selLineMask_input,
                                                    vdotnScalar_input,
                                                    logit_f,
                                                    clineInvMask_input,
                                                    hyper_params['rootFt'],
                                                    is_training=False)

    # Test loss
    test_loss, test_d_loss, test_n_loss, test_ds_loss, test_reg_loss, test_real_dloss, \
    test_real_nloss, test_omega_loss, out_gt_normal, out_f_normal, out_gt_depth, out_f_depth, out_gt_ds, gt_lines, \
    reg_mask, out_cf_map, test_gt_a, test_gt_b, test_f_a, test_f_b \
        = loss(logit_d,
               logit_n,
               logit_c,
               gtNormal_input,
               gtDepth_input,
               maskShape_input,
               maskDs_input,
               clineInvMask_input,
               ds_input,
               nprLine_input,
               logit_f,
               gtField_input,
               fmInv_input)

    return test_loss, test_d_loss, test_n_loss, test_ds_loss, test_reg_loss, test_real_dloss, \
           test_real_nloss, test_omega_loss, out_gt_normal, out_f_normal, out_gt_depth, \
           out_f_depth, out_gt_ds, gt_lines, reg_mask, out_cf_map, test_gt_a, test_gt_b, test_f_a, test_f_b, \
           [npr_lines, ds, fm, fm_inv, gt_normal, gt_depth, gt_field,
            mask_cline_inv, mask_shape, mask_ds, mask2d, selm, ndotv]


def test_net():
    # Set logging
    test_logger = logging.getLogger('main.testing')
    test_logger.info('---Begin testing: ---')

    # Load network
    net = SKETCHNET()

    # Testing data
    data_records = [item for item in os.listdir(hyper_params['dbTest']) if item.endswith('.tfrecords')]
    test_records = [os.path.join(hyper_params['dbTest'], item) for item in data_records if item.find('test') != -1]

    test_loss, test_d_loss, test_n_loss, test_ds_loss, test_r_loss, test_real_dloss, \
    test_real_nloss, test_omega_loss, test_gt_normal, test_f_normal, test_gt_depth, test_f_depth, test_gt_ds, \
    test_gt_lines, test_reg_mask, test_f_cfmap, test_gt_a, test_gt_b, test_f_a, test_f_b, test_inputList \
        = test_procedure(net, test_records)

    # Saver
    tf_saver = tf.train.Saver()
    
    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    config.allow_soft_placement = True
    # config.log_device_placement = True

    with tf.Session(config=config) as sess:
        # initialize
        init_op = tf.group(tf.global_variables_initializer(), tf.local_variables_initializer())
        sess.run(init_op)

        # Restore model
        ckpt = tf.train.latest_checkpoint(hyper_params['cktDir'])
        if ckpt:
            tf_saver.restore(sess, ckpt)
            test_logger.info('restore from the checkpoint {}'.format(ckpt))

        # write graph:
        tf.train.write_graph(sess.graph_def,
                             hyper_params['outDir'],
                             hyper_params['graphName'],
                             as_text=True)
        test_logger.info('save graph tp pbtxt, done')

        # Start input enqueue threads
        coord = tf.train.Coordinator()
        threads = tf.train.start_queue_runners(sess=sess, coord=coord)

        try:
            titr = 0
            avg_loss = 0.0
            while not coord.should_stop():

                # get real input
                test_real_input = sess.run(test_inputList)

                t_loss, t_d_loss, t_n_loss, t_ds_loss, t_r_loss, t_real_dloss, t_real_nloss, \
                t_omega_loss, t_gt_normal, t_f_normal, t_gt_depth, t_f_depth, t_gt_ds, t_gt_lines, t_reg_mask, \
                t_f_cfmap, t_gt_a, t_gt_b, t_f_a, t_f_b \
                    = sess.run([test_loss, test_d_loss, test_n_loss, test_ds_loss, test_r_loss,
                                test_real_dloss, test_real_nloss, test_omega_loss, test_gt_normal, test_f_normal,
                                test_gt_depth, test_f_depth, test_gt_ds, test_gt_lines, test_reg_mask, test_f_cfmap,
                                test_gt_a, test_gt_b, test_f_a, test_f_b],
                               feed_dict={'npr_input:0': test_real_input[0],
                                          'ds_input:0': test_real_input[1],
                                          'fLMask_input:0': test_real_input[2],
                                          'fLInvMask_input:0': test_real_input[3],
                                          'gtN_input:0': test_real_input[4],
                                          'gtD_input:0': test_real_input[5],
                                          'gtField_input:0': test_real_input[6],
                                          'clIMask_input:0': test_real_input[7],
                                          'shapeMask_input:0': test_real_input[8],
                                          'dsMask_input:0': test_real_input[9],
                                          '2dMask_input:0': test_real_input[10],
                                          'sLMask_input:0': test_real_input[11],
                                          'curvMag_input:0': test_real_input[12]
                                          })

                # Record loss
                avg_loss += t_loss
                test_logger.info(
                    'Test case {}, loss: {}, {}, {}, {}, {}, {}, {}, 0.0, {}'.format(titr, t_loss, t_real_dloss,
                                                                                     t_real_nloss, t_d_loss,
                                                                                     t_n_loss,
                                                                                     t_ds_loss, t_r_loss,
                                                                                     t_omega_loss))

                # Write img out
                if titr < 200:
                    fn1 = os.path.join(out_img_dir, 'gt_depth_' + str(titr) + '.exr')
                    fn2 = os.path.join(out_img_dir, 'fwd_depth_' + str(titr) + '.exr')
                    fn3 = os.path.join(out_img_dir, 'gt_normal_' + str(titr) + '.exr')
                    fn4 = os.path.join(out_img_dir, 'fwd_normal_' + str(titr) + '.exr')
                    fn5 = os.path.join(out_img_dir, 'fwd_conf_map_' + str(titr) + '.exr')
                    fn6 = os.path.join(out_img_dir, 'gt_field_a_' + str(titr) + '.exr')
                    fn7 = os.path.join(out_img_dir, 'gt_field_b_' + str(titr) + '.exr')
                    fn8 = os.path.join(out_img_dir, 'fwd_field_a_' + str(titr) + '.exr')
                    fn9 = os.path.join(out_img_dir, 'fwd_field_b_' + str(titr) + '.exr')
    
                    out_gt_d = t_gt_depth[0, :, :, :]
                    out_gt_d.astype(np.float32)
                    out_gt_d = np.flip(out_gt_d, 0)
                    cv2.imwrite(fn1, out_gt_d)
    
                    out_f_d = t_f_depth[0, :, :, :]
                    out_f_d.astype(np.float32)
                    out_f_d = np.flip(out_f_d, 0)
                    cv2.imwrite(fn2, out_f_d)
    
                    out_gt_normal = t_gt_normal[0, :, :, :]
                    out_gt_normal = out_gt_normal[:, :, [2, 1, 0]]
                    out_gt_normal.astype(np.float32)
                    out_gt_normal = np.flip(out_gt_normal, 0)
                    cv2.imwrite(fn3, out_gt_normal)
    
                    out_f_normal = t_f_normal[0, :, :, :]
                    out_f_normal = out_f_normal[:, :, [2, 1, 0]]
                    out_f_normal.astype(np.float32)
                    out_f_normal = np.flip(out_f_normal, 0)
                    cv2.imwrite(fn4, out_f_normal)
    
                    out_f_cfmap = t_f_cfmap[0, :, :, :]
                    out_f_cfmap.astype(np.float32)
                    out_f_cfmap = np.flip(out_f_cfmap, 0)
                    cv2.imwrite(fn5, out_f_cfmap)
    
                    out_gt_a = t_gt_a[0, :, :, :]
                    out_gt_a = out_gt_a[:, :, [2, 1, 0]]
                    out_gt_a.astype(np.float32)
                    out_gt_a = np.flip(out_gt_a, 0)
                    cv2.imwrite(fn6, out_gt_a)
    
                    out_gt_b = t_gt_b[0, :, :, :]
                    out_gt_b = out_gt_b[:, :, [2, 1, 0]]
                    out_gt_b.astype(np.float32)
                    out_gt_b = np.flip(out_gt_b, 0)
                    cv2.imwrite(fn7, out_gt_b)
    
                    out_f_a = t_f_a[0, :, :, :]
                    out_f_a = out_f_a[:, :, [2, 1, 0]]
                    out_f_a.astype(np.float32)
                    out_f_a = np.flip(out_f_a, 0)
                    cv2.imwrite(fn8, out_f_a)
    
                    out_f_b = t_f_b[0, :, :, :]
                    out_f_b = out_f_b[:, :, [2, 1, 0]]
                    out_f_b.astype(np.float32)
                    out_f_b = np.flip(out_f_b, 0)
                    cv2.imwrite(fn9, out_f_b)

                titr += 1
                if titr % 100 == 0:
                    print('Iteration: {}'.format(titr))

            avg_loss /= titr
            test_logger.info('Finish test model, average loss is: {}'.format(avg_loss))

        except tf.errors.OutOfRangeError:
            print('Test Done.')
        finally:
            coord.request_stop()

        # Finish testing
        coord.join(threads)


if __name__ == '__main__':
    # parse arguments
    parser = argparse.ArgumentParser()
    parser.add_argument('--cktDir', required=True, help='checkpoint directory', type=str)
    parser.add_argument('--dbTest', required=True, help='test dataset directory', type=str)
    parser.add_argument('--outDir', required=True, help='otuput directory', type=str)
    parser.add_argument('--device', help='GPU device index', type=str, default='0')
    parser.add_argument('--graphName', required=True, help='writen graph name, net.pbtxt', type=str)

    args = parser.parse_args()
    hyper_params['cktDir'] = args.cktDir
    hyper_params['dbTest'] = args.dbTest
    hyper_params['outDir'] = args.outDir
    hyper_params['device'] = args.device
    hyper_params['graphName'] = args.graphName

    # Set GPU
    os.environ['CUDA_VISIBLE_DEVICES'] = hyper_params['device']

    # out img dir
    out_img_dir = os.path.join(hyper_params['outDir'], 'out_img')
    make_dir(out_img_dir)

    # Set logger
    logger = logging.getLogger('main')
    logger.setLevel(logging.DEBUG)
    fh = logging.FileHandler(os.path.join(hyper_params['outDir'], 'log.txt'))
    fh.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    fh.setFormatter(formatter)
    ch.setFormatter(formatter)
    logger.addHandler(fh)
    logger.addHandler(ch)

    # Training preparation
    logger.info('---Test preparation: ---')

    # Dump parameters
    dump_params(hyper_params['outDir'], hyper_params)

    # Begin training
    test_net()
