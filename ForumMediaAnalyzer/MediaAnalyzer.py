import re
import cv2
import json
import base64
import logging
import requests
import numpy as np
from datetime import datetime

import gridfs
import pymongo
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError as MongoServerSelectionTimeoutError

import imagehash
from PIL import Image
from skimage.metrics import structural_similarity


class AnalyzeConditionsNotMetException(Exception):
    """
    Raised when an error is encountered during execution of the run() function
    """
    pass


class MediaAnalyzer(object):
    """
    This class is used to analyze data generated by a MediaScraper object:
    https://github.com/jesseVDwolf/ForumMediaScraper

    It will retrieve data in batches using the MediaScraper's REST interface:
    https://github.com/jesseVDwolf/ForumMediaScraperREST
    """

    # taken from https://github.com/django/django/blob/stable/1.3.x/django/core/validators.py#L45
    URL_VALIDATION_REGEX = re.compile(
        r'^(?:http|ftp)s?://' # http:// or https://
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)|' #domain...
        r'localhost|' #localhost...
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})' # ...or ip
        r'(?::\d+)?' # optional port
        r'(?:/?|[/?]\S+)$', re.IGNORECASE)

    MONGO_DEFAULT_URI = "mongodb://localhost:27017"

    def __init__(self, scraper_rest_host: str="http://localhost:5000", log_level: int=logging.DEBUG,
                 document_retrieval_batch_size: int=5, mongo_uri: str=MONGO_DEFAULT_URI):
        if re.match(MediaAnalyzer.URL_VALIDATION_REGEX, scraper_rest_host) is None:
            raise ValueError('Invalid scraper_rest_host url: %s' % scraper_rest_host)

        self.scraper_rest_host = scraper_rest_host
        self.document_retrieval_batch_size = document_retrieval_batch_size

        # create database related objects
        self._mongo_client = MongoClient(mongo_uri)
        self._mongo_database = self._mongo_client['9GagMedia']
        self.gridfs = gridfs.GridFS(self._mongo_database)

        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(log_level)
        logging_args = {
            "format": '%(asctime)s %(levelname)-8s %(message)s',
            "level": logging.INFO,
            "datefmt": '%Y-%m-%d %H:%M:%S'
        }
        logging.basicConfig(**logging_args)

        if not self._mongo_database['Counter'].find_one():
            self._mongo_database['Counter'].insert_one({'_id': 'OrderNum', 'val': 1})

    @staticmethod
    def _scale_images(image_one: np.ndarray, image_two: np.ndarray, scale_percent_dif: float=0.02):
        # Scale the images so that they have the same
        # dimensions. The bigger image will always be scaled down;
        # It is considered bigger if contains more pixels i.e width x height
        if image_one.shape == image_two.shape:
            return image_one, image_two

        if abs((image_one[0] / image_one[1]) - (image_two[0] / image_two[1])) >= scale_percent_dif:
            return None, None

        if sum(image_one[:2]) > sum(image_two[:2]):
            image_one = cv2.resize(
                src=image_one,
                dsize=(image_two.shape[1], image_two.shape[0]),
                interpolation=cv2.INTER_CUBIC
            )
        else:
            image_two = cv2.resize(
                src=image_two,
                dsize=(image_one.shape[1], image_one.shape[0]),
                interpolation=cv2.INTER_CUBIC
            )
        return image_one, image_two

    @staticmethod
    def _mse(image_one: np.ndarray, image_two: np.ndarray):
        # the 'Mean Squared Error' between the two images is the
        # sum of the squared difference between the two images;
        # NOTE: the two images must have the same dimension
        err = np.sum((image_one.astype("float") - image_two.astype("float")) ** 2)
        err /= float(image_one.shape[0] * image_one.shape[1])

        # return the MSE, the lower the error, the more "similar"
        # the two images are
        return err

    @staticmethod
    def _img_hash(image_one: np.ndarray, image_two: np.ndarray, func=imagehash.average_hash, cutoff: int=10):
        # Use an image hashing algorithm to check for similarity between images
        # Calculate the hashes of both images using one of the functions from
        # the https://github.com/JohannesBuchner/imagehash project and subtract
        # them from each other. A cutoff can be specified to account for
        # little discrepancies
        h1 = func(Image.fromarray(image_one))
        h2 = func(Image.fromarray(image_two))
        s = (h1 - h2) - cutoff

        # return the similarity between images where the closer to 0 the better.
        # taking into account the specified cutoff where s can not be a negative number
        return int((abs(s)+s)/2)

    def run(self):
        try:
            """
            Pre-run validation of resources on scraper rest interface and 
            the locally configured mongodb server
            """
            r = requests.get(
                url="%s/query" % self.scraper_rest_host,
                params={'limit': 1, 'offset': 0}
            )
            r.raise_for_status()
            self._mongo_client.server_info()

            """
            Start processing. If posts have already been processed, use the ArticleId of the 
            last processed article to determine when to stop retrieving more data. Then use 
            different methods to determine similairity between images:
            - image hashes
            - mean squared error
            - structural similarity measure
            """
            last_article = self._mongo_database['Posts'].find_one(sort=[("OrderNum", pymongo.ASCENDING)])
            run = self._mongo_database['Runs'].insert_one({
                'StartProcessTime': datetime.utcnow(),
                'EndProcessTime': None,
                'PostsProcessed': 0,
                'BatchesProcessed': 0
            })
            request_offset = 0
            final_batch = False
            last_article_found = False
            save_all = False if last_article else True
            posts_processed = 0
            batches_processed = 0

            while True:
                resp = requests.get(url="%s/query" % self.scraper_rest_host, params={
                    'limit': self.document_retrieval_batch_size,
                    'offset': request_offset
                })
                resp.raise_for_status()
                data = resp.json()
                self.logger.debug('%s: Received new batch of data at %s using offset %d and limit %d' % (
                    str(run.inserted_id), datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), request_offset, self.document_retrieval_batch_size))

                if len(data['documents']) == 0:
                    self.logger.debug('%s: No more documents returned by %s using offset %d and limit %d' % (
                        str(run.inserted_id), self.scraper_rest_host, request_offset, self.document_retrieval_batch_size))
                    self.logger.info('%s: No more documents found. Finished %d batches' % (str(run.inserted_id), batches_processed))
                    break

                if len(data['documents']) < self.document_retrieval_batch_size:
                    self.logger.debug('%s: No more data available from %s. Setting final batch' % (
                        str(run.inserted_id), self.scraper_rest_host))
                    final_batch = True

                if len([doc for doc in data['documents'] if len(doc['Posts']) == 0]) == len(data['documents']):
                    self.logger.debug('%s: No posts found in documents at offset %d with limit %d' % (
                        str(run.inserted_id), request_offset, self.document_retrieval_batch_size))
                    self.logger.info('%s: No posts found in batch. Retrieving next batch' % str(run.inserted_id))
                    request_offset += self.document_retrieval_batch_size
                    batches_processed += 1
                    continue

                if save_all:
                    for doc in [doc for doc in data['documents'] if len(doc['Posts']) != 0]:
                        for post in doc['Posts']:
                            s = str(post['MediaData'])
                            d = base64.b64decode(s.encode('utf-8'))
                            buff = np.asarray(bytearray(d), dtype=np.uint8)
                            im = cv2.imdecode(buff, cv2.IMREAD_GRAYSCALE)
                            media_id = self.gridfs.put(d)
                            order_num = self._mongo_database['Counter'].find_one().get('val')
                            md = {
                                "ArticleId": str(post['ArticleId']),
                                "OrderNum": int(order_num),
                                "RunId": run.inserted_id,
                                "PostProcessedTime": datetime.utcnow(),
                                "Dim": im.shape,
                                "Media": media_id,
                                "IsOriginal": True,
                                "RepostOff": None,
                                "Reposts": []
                            }
                            self._mongo_database['Posts'].insert_one(md)
                            self._mongo_database['Counter'].update({'_id': 'OrderNum'}, {'$inc': {'val': 1}})
                            posts_processed += 1

                else:
                    for doc in [doc for doc in data['documents'] if len(doc['Posts']) != 0]:
                        if last_article['ArticleId'] == doc['StartPostId'] or last_article_found:
                            self.logger.debug('%s: Last article %s found at offset %d with limit %d' % (
                                str(run.inserted_id), str(last_article['ArticleId']), request_offset, self.document_retrieval_batch_size))
                            final_batch = True
                            break

                        for post in doc['Posts']:
                            if last_article['ArticleId'] == post['ArticleId']:
                                self.logger.debug('%s: Last article %s found at offset %d with limit %d' % (
                                    str(run.inserted_id), str(last_article['ArticleId']), request_offset, self.document_retrieval_batch_size))
                                last_article_found = True
                                break

                            s = str(post['MediaData'])
                            d = base64.b64decode(s.encode('utf-8'))
                            buff = np.asarray(bytearray(d), dtype=np.uint8)
                            im = cv2.imdecode(buff, cv2.IMREAD_GRAYSCALE)
                            media_id = self.gridfs.put(d)
                            md = {
                                "ArticleId": str(post['ArticleId']),
                                "RunId": run.inserted_id,
                                "PostProcessedTime": datetime.utcnow(),
                                "Dim": im.shape,
                                "MediaId": media_id,
                                "IsOriginal": True,
                                "RepostOff": None,
                                "Reposts": []
                            }
                            processed_posts = self._mongo_database['Posts'].find({})

                            for pp in processed_posts:
                                f = self.gridfs.get(pp['MediaId'])
                                buff = np.asarray(bytearray(f.read(size=-1)), dtype=np.uint8)
                                im0 = cv2.imdecode(buff, cv2.IMREAD_GRAYSCALE)
                                im, im0 = self._scale_images(im, im0)
                                if not im:
                                    # images could not be scaled since difference in dimensions
                                    # is too big. Must be unique based on this
                                    continue

                                mse = self._mse(im, im0)
                                ss = structural_similarity(im, im0)
                                hs = self._img_hash(im, im0)

                                # The hash similarity will determine if an image is even close to being
                                # similar to the processed image. The structural similarity measure will
                                # then decide if this is actually correct. A last check is done to make
                                # sure that its not a meme that is posted with the same background but
                                # with different text using the very sensitive mse measure
                                if hs == 0:
                                    if ss >= 0.75:
                                        if not mse >= 2000.00 and pp['IsOriginal']:
                                            # db image seems to be very similar to the processed image
                                            md.update({"IsOriginal": False, "RepostOff": pp['_id'], "Reposts": None})
                                            pp['Reposts'].append({
                                                "ArticleId": md['ArticleId'],
                                                "mse": mse,
                                                "ssim": ss,
                                                "hs": hs,
                                                "certainty": 1
                                            })
                                            self._mongo_database['Posts'].replace_one({"_id": pp['_id']}, pp)
                                        else:
                                            # image background might be the same with different text
                                            continue
                                    else:
                                        # structural similarity is too far off must be unique
                                        continue
                                else:
                                    # images are not similar at all
                                    continue

                            # insert data into mongo
                            self._mongo_database['Posts'].insert_one(md)

                if final_batch:
                    break

                request_offset += self.document_retrieval_batch_size

        except requests.exceptions.RequestException as re:
            raise AnalyzeConditionsNotMetException({'message': re})
        except MongoServerSelectionTimeoutError as msste:
            raise AnalyzeConditionsNotMetException({'message': msste})
        except json.JSONDecodeError as je:
            raise AnalyzeConditionsNotMetException({'message': je})
