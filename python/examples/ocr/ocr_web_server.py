# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from paddle_serving_client import Client
from paddle_serving_app.reader import OCRReader
import cv2
import sys
import numpy as np
import os
from paddle_serving_client import Client
from paddle_serving_app.reader import Sequential, URL2Image, ResizeByFactor
from paddle_serving_app.reader import Div, Normalize, Transpose
from paddle_serving_app.reader import DBPostProcess, FilterBoxes
from paddle_serving_server_gpu.web_service import WebService
import time
import re


class OCRService(WebService):
    def init_det_client(self, det_port, det_client_config):
        self.det_preprocess = Sequential([
            ResizeByFactor(32, 960), Div(255),
            Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]), Transpose(
                (2, 0, 1))
        ])
        self.det_client = Client()
        self.det_client.load_client_config(det_client_config)
        self.det_client.connect(["127.0.0.1:{}".format(det_port)])

    def preprocess(self, feed=[], fetch=[]):
        img_url = feed[0]["image"]
        #print(feed, img_url)
        read_from_url = URL2Image()
        im = read_from_url(img_url)
        ori_h, ori_w, _ = im.shape
        det_img = self.det_preprocess(im)
        #print("det_img", det_img, det_img.shape)
        det_out = self.det_client.predict(
            feed={"image": det_img}, fetch=["concat_1.tmp_0"])

        #print("det_out", det_out)
        def sorted_boxes(dt_boxes):
            num_boxes = dt_boxes.shape[0]
            sorted_boxes = sorted(dt_boxes, key=lambda x: (x[0][1], x[0][0]))
            _boxes = list(sorted_boxes)
            for i in range(num_boxes - 1):
                if abs(_boxes[i+1][0][1] - _boxes[i][0][1]) < 10 and \
                    (_boxes[i + 1][0][0] < _boxes[i][0][0]):
                    tmp = _boxes[i]
                    _boxes[i] = _boxes[i + 1]
                    _boxes[i + 1] = tmp
            return _boxes

        def get_rotate_crop_image(img, points):
            img_height, img_width = img.shape[0:2]
            left = int(np.min(points[:, 0]))
            right = int(np.max(points[:, 0]))
            top = int(np.min(points[:, 1]))
            bottom = int(np.max(points[:, 1]))
            img_crop = img[top:bottom, left:right, :].copy()
            points[:, 0] = points[:, 0] - left
            points[:, 1] = points[:, 1] - top
            img_crop_width = int(np.linalg.norm(points[0] - points[1]))
            img_crop_height = int(np.linalg.norm(points[0] - points[3]))
            pts_std = np.float32([[0, 0], [img_crop_width, 0], \
                                  [img_crop_width, img_crop_height], [0, img_crop_height]])
            M = cv2.getPerspectiveTransform(points, pts_std)
            dst_img = cv2.warpPerspective(
                img_crop,
                M, (img_crop_width, img_crop_height),
                borderMode=cv2.BORDER_REPLICATE)
            dst_img_height, dst_img_width = dst_img.shape[0:2]
            if dst_img_height * 1.0 / dst_img_width >= 1.5:
                dst_img = np.rot90(dst_img)
            return dst_img

        def resize_norm_img(img, max_wh_ratio):
            import math
            imgC, imgH, imgW = 3, 32, 320
            imgW = int(32 * max_wh_ratio)
            h = img.shape[0]
            w = img.shape[1]
            ratio = w / float(h)
            if math.ceil(imgH * ratio) > imgW:
                resized_w = imgW
            else:
                resized_w = int(math.ceil(imgH * ratio))
            resized_image = cv2.resize(img, (resized_w, imgH))
            resized_image = resized_image.astype('float32')
            resized_image = resized_image.transpose((2, 0, 1)) / 255
            resized_image -= 0.5
            resized_image /= 0.5
            padding_im = np.zeros((imgC, imgH, imgW), dtype=np.float32)
            padding_im[:, :, 0:resized_w] = resized_image
            return padding_im

        _, new_h, new_w = det_img.shape
        filter_func = FilterBoxes(10, 10)
        post_func = DBPostProcess({
            "thresh": 0.3,
            "box_thresh": 0.5,
            "max_candidates": 1000,
            "unclip_ratio": 1.5,
            "min_size": 3
        })
        ratio_list = [float(new_h) / ori_h, float(new_w) / ori_w]
        dt_boxes_list = post_func(det_out["concat_1.tmp_0"], [ratio_list])
        dt_boxes = filter_func(dt_boxes_list[0], [ori_h, ori_w])
        dt_boxes = sorted_boxes(dt_boxes)
        feed_list = []
        img_list = []
        max_wh_ratio = 0
        for i, dtbox in enumerate(dt_boxes):
            boximg = get_rotate_crop_image(im, dt_boxes[i])
            img_list.append(boximg)
            h, w = boximg.shape[0:2]
            wh_ratio = w * 1.0 / h
            max_wh_ratio = max(max_wh_ratio, wh_ratio)
        for img in img_list:
            norm_img = resize_norm_img(img, max_wh_ratio)
            feed = {"image": norm_img}
            feed_list.append(feed)
        fetch = ["ctc_greedy_decoder_0.tmp_0"]
        #print("feed_list", feed_list)
        return feed_list, fetch

    def postprocess(self, feed={}, fetch=[], fetch_map=None):
        #print(fetch_map)
        ocr_reader = OCRReader()
        rec_res = ocr_reader.postprocess(fetch_map)
        res_lst = []
        for res in rec_res:
            res_lst.append(res[0])
        fetch_map["res"] = res_lst
        del fetch_map["ctc_greedy_decoder_0.tmp_0"]
        del fetch_map["ctc_greedy_decoder_0.tmp_0.lod"]
        return fetch_map


ocr_service = OCRService(name="ocr")
ocr_service.load_model_config("ocr_rec_model")
ocr_service.prepare_server(workdir="workdir", port=9292)
ocr_service.init_det_client(
    det_port=9293,
    det_client_config="ocr_det_client/serving_client_conf.prototxt")
ocr_service.run_rpc_service()
ocr_service.run_web_service()
