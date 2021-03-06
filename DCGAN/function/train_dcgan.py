import cv2
import time
import argparse
import logging
import pprint
import os
import sys
import matplotlib
matplotlib.use('Agg')
from config.config import config, update_config
from symbols.mxnet_dcgan import get_symbol_generator, get_symbol_discriminator
from core.dataset import get_mnist
from core.create_logger import create_logger
from core.loader import RandIter
from core.visualize import visualize
from core import metric

def parse_args():
    parser = argparse.ArgumentParser(description='Train DCGAN')
    # general
    parser.add_argument('--cfg', help='experiment configure file name', required=True, type=str)

    args, rest = parser.parse_known_args()
    # update config
    update_config(args.cfg)

    args = parser.parse_args()
    return args

args = parse_args()
curr_path = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(curr_path, '../../external/mxnet', config.MXNET_VERSION))

import shutil
import numpy as np
import mxnet as mx


def main():
    # =============setting============
    dataset = config.dataset.dataset
    batch_size = config.TRAIN.BATCH_SIZE
    Z = 100
    lr = config.TRAIN.lr
    beta1 = config.TRAIN.beta1
    sigma = 0.02
    ctx = [mx.gpu(int(i)) for i in config.gpus.split(',')]
    assert len(ctx) == 1
    ctx = ctx[0]
    frequent = config.default.frequent
    check_point = True

    logger, final_output_path = create_logger(config.output_path, args.cfg)
    prefix = os.path.join(final_output_path, config.TRAIN.model_prefix)
    train_fig_path = os.path.join(final_output_path, 'train_fig')

    train_fig_prefix = os.path.join(train_fig_path, dataset)

    if not os.path.exists(train_fig_path):
        os.makedirs(train_fig_path)

    mx.random.seed(config.RNG_SEED)
    np.random.seed(config.RNG_SEED)

    # ==============data==============
    if dataset == 'mnist':
        X_train, X_test = get_mnist()
        train_iter = mx.io.NDArrayIter(X_train, batch_size=batch_size)
    else:
        raise NotImplemented

    rand_iter = RandIter(batch_size, Z)
    label = mx.nd.zeros((batch_size,), ctx=ctx)

    # print config
    pprint.pprint(config)
    logger.info('system:{}'.format(os.uname()))
    logger.info('mxnet path:{}'.format(mx.__file__))
    logger.info('rng seed:{}'.format(config.RNG_SEED))
    logger.info('training config:{}\n'.format(pprint.pformat(config)))

    # =============Generator Module=============
    generatorSymbol = get_symbol_generator()
    generator = mx.mod.Module(symbol=generatorSymbol, data_names=('rand',), label_names=None, context=ctx)
    generator.bind(data_shapes=rand_iter.provide_data)
    generator.init_params(initializer=mx.init.Normal(sigma))
    generator.init_optimizer(
        optimizer='adam',
        optimizer_params={
            'learning_rate': lr,
            'beta1': beta1,
        })
    mods = [generator]

    # =============Discriminator Module=============
    discriminatorSymbol = get_symbol_discriminator()
    discriminator = mx.mod.Module(symbol=discriminatorSymbol, data_names=('data',), label_names=('label',), context=ctx)
    discriminator.bind(data_shapes=train_iter.provide_data,
                       label_shapes=[('label', (batch_size,))],
                       inputs_need_grad=True)
    discriminator.init_params(initializer=mx.init.Normal(sigma))
    discriminator.init_optimizer(
        optimizer='adam',
        optimizer_params={
            'learning_rate': lr,
            'beta1': beta1,
        })
    mods.append(discriminator)

    # metric
    # mG = mx.metric.CustomMetric(metric.fentropy)
    # mD = mx.metric.CustomMetric(metric.fentropy)
    # mACC = mx.metric.CustomMetric(metric.facc)
    # test_metric = metric.CrossEntropyMetric()
    # test_metric.reset()
    mG = metric.CrossEntropyMetric()
    mD = metric.CrossEntropyMetric()
    mACC = metric.AccMetric()

    # =============train===============
    for epoch in range(config.TRAIN.end_epoch):
        train_iter.reset()
        mACC.reset()
        mG.reset()
        mD.reset()
        for t, batch in enumerate(train_iter):
            rbatch = rand_iter.next()

            generator.forward(rbatch, is_train=True)
            outG = generator.get_outputs()

            # update discriminator on fake
            label[:] = 0
            discriminator.forward(mx.io.DataBatch(outG, [label]), is_train=True)
            discriminator.backward()
            gradD = [[grad.copyto(grad.context) for grad in grads] for grads in discriminator._exec_group.grad_arrays]

            discriminator.update_metric(mD, [label])
            discriminator.update_metric(mACC, [label])
            # test_metric.update([label], discriminator.get_outputs())

            # update discriminator on real
            label[:] = 1
            batch.label = [label]
            discriminator.forward(batch, is_train=True)
            discriminator.backward()
            for gradsr, gradsf in zip(discriminator._exec_group.grad_arrays, gradD):
                for gradr, gradf in zip(gradsr, gradsf):
                    gradr += gradf
            discriminator.update()

            discriminator.update_metric(mD, [label])
            discriminator.update_metric(mACC, [label])
            # test_metric.update([label], discriminator.get_outputs())

            # update generator
            label[:] = 1
            discriminator.forward(mx.io.DataBatch(outG, [label]), is_train=True)
            discriminator.backward()
            diffD = discriminator.get_input_grads()
            generator.backward(diffD)
            generator.update()

            mG.update([label], discriminator.get_outputs())

            t += 1
            if t % frequent == 0:
                # visualize(outG[0].asnumpy(), batch.data[0].asnumpy())
                print 'Epoch[{}] Batch[{}] dACC: {:.4f} gCE: {:.4f} dCE: {:.4f}'.format(epoch, t, mACC.get()[1], mG.get()[1], mD.get()[1])
                logger.info('Epoch[{}] Batch[{}] dACC: {:.4f} gCE: {:.4f} dCE: {:.4f}\n'.format(epoch, t, mACC.get()[1], mG.get()[1], mD.get()[1]))

        if check_point:
            print('Saving...')
            visualize(outG[0].asnumpy(), batch.data[0].asnumpy(), train_fig_prefix + '-train-%04d.png' % (epoch + 1))
            generator.save_params(prefix + '-generator-%04d.params' % (epoch + 1))
            discriminator.save_params(prefix + '-discriminator-%04d.params' % (epoch + 1))


