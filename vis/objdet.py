import argparse
import os
import cv2
import mxnet as mx
import numpy as np
from vis.rcnn.logger import logger
from vis.rcnn.config import config
from vis.rcnn.symbol import get_vgg_test, get_vgg_rpn_test, get_resnet_test
from rcnn.io.image import resize, transform
from rcnn.core.tester import Predictor, im_detect, im_proposal, vis_all_detection, draw_all_detection
from rcnn.utils.load_model import load_param
from rcnn.processing.nms import py_nms_wrapper, cpu_nms_wrapper, gpu_nms_wrapper
import simplejson as json
import jsonpickle
from urllib2 import Request, urlopen, HTTPError, URLError

CLASSES = ('__background__',
           'aeroplane', 'bicycle', 'bird', 'boat',
           'bottle', 'bus', 'car', 'cat', 'chair',
           'cow', 'diningtable', 'dog', 'horse',
           'motorbike', 'person', 'pottedplant',
           'sheep', 'sofa', 'train', 'tvmonitor')

config.TEST.HAS_RPN = True
SHORT_SIDE = config.SCALES[0][0]
LONG_SIDE = config.SCALES[0][1]
PIXEL_MEANS = config.PIXEL_MEANS
DATA_NAMES = ['data', 'im_info']
LABEL_NAMES = None
DATA_SHAPES = [('data', (1, 3, LONG_SIDE, SHORT_SIDE)), ('im_info', (1, 3))]
LABEL_SHAPES = None
# visualization
CONF_THRESH = 0.7
NMS_THRESH = 0.3
nms = py_nms_wrapper(NMS_THRESH)

class NumpyFloatHandler(jsonpickle.handlers.BaseHandler):
    """
    Automatic conversion of numpy float  to python floats
    Required for jsonpickle to work correctly
    """
    def flatten(self, obj, data):
        """
        Converts and rounds a Numpy.float* to Python float
        """
        return round(obj,6)

def get_net(symbol, prefix, epoch, ctx):
    arg_params, aux_params = load_param(prefix, epoch, convert=True, ctx=ctx, process=True)

    # infer shape
    data_shape_dict = dict(DATA_SHAPES)
    arg_names, aux_names = symbol.list_arguments(), symbol.list_auxiliary_states()
    arg_shape, _, aux_shape = symbol.infer_shape(**data_shape_dict)
    arg_shape_dict = dict(zip(arg_names, arg_shape))
    aux_shape_dict = dict(zip(aux_names, aux_shape))

    # check shapes
    for k in symbol.list_arguments():
        if k in data_shape_dict or 'label' in k:
            continue
        assert k in arg_params, k + ' not initialized'
        assert arg_params[k].shape == arg_shape_dict[k], \
            'shape inconsistent for ' + k + ' inferred ' + str(arg_shape_dict[k]) + ' provided ' + str(arg_params[k].shape)
    for k in symbol.list_auxiliary_states():
        assert k in aux_params, k + ' not initialized'
        assert aux_params[k].shape == aux_shape_dict[k], \
            'shape inconsistent for ' + k + ' inferred ' + str(aux_shape_dict[k]) + ' provided ' + str(aux_params[k].shape)

    predictor = Predictor(symbol, DATA_NAMES, LABEL_NAMES, context=ctx,
                          provide_data=DATA_SHAPES, provide_label=LABEL_SHAPES,
                          arg_params=arg_params, aux_params=aux_params)
    return predictor


def generate_batch(im):
    """
    preprocess image, return batch
    :param im: cv2.imread returns [height, width, channel] in BGR
    :return:
    data_batch: MXNet input batch
    data_names: names in data_batch
    im_scale: float number
    """
    im_array, im_scale = resize(im, SHORT_SIDE, LONG_SIDE)
    im_array = transform(im_array, PIXEL_MEANS)
    im_info = np.array([[im_array.shape[2], im_array.shape[3], im_scale]], dtype=np.float32)
    data = [mx.nd.array(im_array), mx.nd.array(im_info)]
    data_shapes = [('data', im_array.shape), ('im_info', im_info.shape)]
    data_batch = mx.io.DataBatch(data=data, label=None, provide_data=data_shapes, provide_label=None)
    return data_batch, DATA_NAMES, im_scale


def demo_net(predictor, image_name, vis=False):
    """
    generate data_batch -> im_detect -> post process
    :param predictor: Predictor
    :param image_name: image name
    :param vis: will save as a new image if not visualized
    :return: None
    """
    assert os.path.exists(image_name), image_name + ' not found'
    im = cv2.imread(image_name)
    data_batch, data_names, im_scale = generate_batch(im)
    scores, boxes, data_dict = im_detect(predictor, data_batch, data_names, im_scale)

    all_boxes = [[] for _ in CLASSES]
    for cls in CLASSES:
        cls_ind = CLASSES.index(cls)
        cls_boxes = boxes[:, 4 * cls_ind:4 * (cls_ind + 1)]
        cls_scores = scores[:, cls_ind, np.newaxis]
        keep = np.where(cls_scores >= CONF_THRESH)[0]
        dets = np.hstack((cls_boxes, cls_scores)).astype(np.float32)[keep, :]
        keep = nms(dets)
        all_boxes[cls_ind] = dets[keep, :]

    boxes_this_image = [[]] + [all_boxes[j] for j in range(1, len(CLASSES))]

    # print results
    logger.info('---class---')
    logger.info('[[x1, x2, y1, y2, confidence]]')
    for ind, boxes in enumerate(boxes_this_image):
        if len(boxes) > 0:
            logger.info('---%s---' % CLASSES[ind])
            logger.info('%s' % boxes)

    if vis:
        vis_all_detection(data_dict['data'].asnumpy(), boxes_this_image, CLASSES, im_scale)
    else:
        result_file = image_name.replace('.', '_result.')
        logger.info('results saved to %s' % result_file)
        im = draw_all_detection(data_dict['data'].asnumpy(), boxes_this_image, CLASSES, im_scale)
        cv2.imwrite(result_file, im)


def parse_args():
    parser = argparse.ArgumentParser(description='Demonstrate a Faster R-CNN network')
    parser.add_argument('--image', help='custom image', type=str)
    parser.add_argument('--prefix', help='saved model prefix', type=str)
    parser.add_argument('--epoch', help='epoch of pretrained model', type=int)
    parser.add_argument('--gpu', help='GPU device to use', default=0, type=int)
    parser.add_argument('--vis', help='display result', action='store_true')
    args = parser.parse_args()
    return args


def downloadModel(modelType):
    if modelType == "resnet":
        url = "https://siastore.blob.core.windows.net/demo/models/rcnn/resnet-0010.params"
        filename = "resnet-0010.params"
    elif modelType == "resnet":
        url = "https://siastore.blob.core.windows.net/demo/models/rcnn/vgg-0010.params"
        filename = "vgg-0010.params"

    saveFolder = "./data/__vision/weights/"
    req = Request(url)
    if os.path.exists(saveFolder + filename):
        return

    # Open the url
    try:
        f = urlopen(req)
        print "downloading " + url

        with open(saveFolder + filename, "wb") as local_file:
            local_file.write(f.read())

    # handle errors
    except HTTPError, e:
        print "HTTP Error:", e.code, url
    except URLError, e:
        print "URL Error:", e.reason, url

def loadModel(modelType, epoch, isgpu):
    downloadModel(modelType)
    if isgpu:
        ctx = mx.gpu()
    else:
        ctx = mx.cpu()

    if modelType == "resnet":
        symbol = get_resnet_test(num_classes=config.NUM_CLASSES)
    elif modelType == "vgg":
        symbol = get_vgg_test(num_classes=config.NUM_CLASSES, num_anchors=config.NUM_ANCHORS)

    predictor = get_net(symbol, modelType, epoch, ctx)
    return predictor

def predict(imagePath, predictor):
    assert os.path.exists(imagePath), imagePath + ' not found'
    im = cv2.imread(imagePath)
    data_batch, data_names, im_scale = generate_batch(im)
    scores, boxes, data_dict = im_detect(predictor, data_batch, data_names, im_scale)
    all_boxes = [[] for _ in CLASSES]
    for cls in CLASSES:
        cls_ind = CLASSES.index(cls)
        cls_boxes = boxes[:, 4 * cls_ind:4 * (cls_ind + 1)]
        cls_scores = scores[:, cls_ind, np.newaxis]
        keep = np.where(cls_scores >= CONF_THRESH)[0]
        dets = np.hstack((cls_boxes, cls_scores)).astype(np.float32)[keep, :]
        keep = nms(dets)
        all_boxes[cls_ind] = dets[keep, :]

    boxes_this_image = [[]] + [all_boxes[j] for j in range(1, len(CLASSES))]
    result = []
    
    # print results
    for ind, boxes in enumerate(boxes_this_image):
        if len(boxes) > 0:
            result.append({"object_name": CLASSES[ind], "confidence": boxes[0][4] ,"bounding_box": {"x1": boxes[0][0], "x2": boxes[0][1], "y1": boxes[0][2], "y2": boxes[0][3]}})

    jsonpickle.handlers.registry.register(np.float, NumpyFloatHandler)
    jsonpickle.handlers.registry.register(np.float32, NumpyFloatHandler)
    jsonpickle.handlers.registry.register(np.float64, NumpyFloatHandler)
    result = jsonpickle.encode(result, unpicklable=False)
    return json.loads(result)

