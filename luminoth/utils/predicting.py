import json
import numpy as np
import os
import tensorflow as tf

from luminoth.models import get_model
from luminoth.datasets import get_dataset


class PredictorNetwork(object):
    """Instantiates a network in order to get predictions from it.

    If a checkpoint exists in the job's directory, load it.  The names of the
    classes will be obtained from the dataset directory.

    Returns a list of objects detected, which is a dict of its coordinates,
    label and probability, ordered by probability.
    """

    def __init__(self, config):

        if config.dataset.dir:
            # Gets the names of the classes
            classes_file = os.path.join(config.dataset.dir, 'classes.json')
            if tf.gfile.Exists(classes_file):
                self.class_labels = json.load(tf.gfile.GFile(classes_file))
            else:
                self.class_labels = None

        # Don't use data augmentation in predictions
        config.dataset.data_augmentation = None

        dataset_class = get_dataset(config.dataset.type)
        model_class = get_model(config.model.type)
        dataset = dataset_class(config)
        model = model_class(config)

        graph = tf.Graph()
        self.session = tf.Session(graph=graph)

        with graph.as_default():
            self.image_placeholder = tf.placeholder(
                tf.float32, (None, None, 3)
            )
            image_tf, _, process_meta = dataset.preprocess(
                self.image_placeholder
            )
            pred_dict = model(image_tf)

            # Restore checkpoint
            if config.train.job_dir:
                job_dir = config.train.job_dir
                if config.train.run_name:
                    job_dir = os.path.join(job_dir, config.train.run_name)
                ckpt = tf.train.get_checkpoint_state(job_dir)
                if not ckpt or not ckpt.all_model_checkpoint_paths:
                    raise ValueError('Could not find checkpoint in {}.'.format(
                        job_dir
                    ))
                ckpt = ckpt.all_model_checkpoint_paths[-1]
                saver = tf.train.Saver(sharded=True, allow_empty=True)
                saver.restore(self.session, ckpt)
                tf.logging.info('Loaded checkpoint.')
            else:
                # A prediction without checkpoint is just used for testing
                tf.logging.warning(
                    'Could not load checkpoint. Using initialized model.')
                init_op = tf.group(
                    tf.global_variables_initializer(),
                    tf.local_variables_initializer()
                )
                self.session.run(init_op)

            if config.model.type == 'ssd':
                cls_prediction = pred_dict['classification_prediction']
                objects_tf = cls_prediction['objects']
                objects_labels_tf = cls_prediction['labels']
                objects_labels_prob_tf = cls_prediction['probs']
            elif config.model.type == 'fasterrcnn':
                if config.model.network.get('with_rcnn', False):
                    cls_prediction = pred_dict['classification_prediction']
                    objects_tf = cls_prediction['objects']
                    objects_labels_tf = cls_prediction['labels']
                    objects_labels_prob_tf = cls_prediction['probs']
                else:
                    rpn_prediction = pred_dict['rpn_prediction']
                    objects_tf = rpn_prediction['proposals']
                    objects_labels_prob_tf = rpn_prediction['scores']
                    # All labels without RCNN are zero
                    objects_labels_tf = tf.zeros(
                        tf.shape(objects_labels_prob_tf), dtype=tf.int32
                    )
            else:
                raise ValueError(
                    "Model type '{}' not supported".format(config.model.type)
                )

            self.fetches = {
                'objects': objects_tf,
                'labels': objects_labels_tf,
                'probs': objects_labels_prob_tf,
                'scale_factor': process_meta['scale_factor']
            }

            # If in debug mode, return the full prediction dictionary.
            if config.train.debug:
                self.fetches['_debug'] = pred_dict

    def bbs_pixel_apart(self, obj, objects):
        repeated_indices = [
            index for index, each_obj in enumerate(
                objects) if [0, 1] == np.unique(
                np.fabs(np.subtract(each_obj, obj))).tolist()]
        return repeated_indices

    def predict_image(self, image):
        fetched = self.session.run(self.fetches, feed_dict={
            self.image_placeholder: np.array(image)
        })

        objects = fetched['objects']
        labels = fetched['labels'].tolist()
        probs = fetched['probs'].tolist()
        scale_factor = fetched['scale_factor']

        if self.class_labels is not None:
            labels = [self.class_labels[label] for label in labels]

        # Scale objects to original image dimensions
        if isinstance(scale_factor, tuple):
            # If scale factor is a tuple, it means we need to scale height and
            # width by a different amount. In that case scale factor is:
            # (scale_factor_height, scale_factor_width)
            objects /= [scale_factor[1], scale_factor[0],
                        scale_factor[1], scale_factor[0]]
        else:
            # If scale factor is a scalar, height and width get scaled by the
            # same amount
            objects /= scale_factor

        # Cast to int to consistently return the same type in Python 2 and 3
        objects = [
            [int(round(coord)) for coord in obj]
            for obj in objects.tolist()
        ]

        # Save a prediction by suppressing the class with
        # lowest probability for the same bounding box
        predictions = [None] * len(objects)
        assert len(objects) == len(labels) == len(probs)
        count = 0
        for obj, label, prob in zip(objects, labels, probs):
            repeated_indices = self.bbs_pixel_apart(obj, objects)
            tf.logging.info("{}".format(repeated_indices))
            if len(repeated_indices) > 0:
                repeated_probs = [probs[i] for i in repeated_indices]
                repeated_probs.append(prob)
                repeated_indices.append(count)
                max_prob = max(repeated_probs)
                tf.logging.info("{} {}".format(
                    repeated_probs, repeated_indices))
                assert len(repeated_probs) == len(repeated_indices)
                prob_index = [
                    index for index, prob in zip(
                        repeated_indices, repeated_probs)
                    if prob == max_prob][0]
                d = {
                    'bbox': objects[prob_index],
                    'label': labels[prob_index],
                    'prob': round(max_prob, 4)}
                tf.logging.info("{} {}".format(count, d))
                tf.logging.info("{} {}".format(prob_index, max_prob))
                tf.logging.info("{}".format(predictions))
                predictions[prob_index] = d
            if objects.count(obj) == 1:
                d = {
                    'bbox': obj,
                    'label': label,
                    'prob': round(prob, 4)}
                predictions[count] = d
            elif objects.count(obj) > 1:
                prob_repeated_objs = [
                    [i, probs[i]] for i, value in enumerate(objects)
                    if value == obj]
                repeated_indices = [i for (i, _) in prob_repeated_objs]
                repeated_probs = [j for (_, j) in prob_repeated_objs]
                max_prob = max(repeated_probs)
                prob_index = [
                    index for index, prob in zip(
                        repeated_indices, repeated_probs)
                    if prob == max_prob][0]
                d = {
                    'bbox': obj,
                    'label': labels[prob_index],
                    'prob': round(max_prob, 4)}
                predictions[prob_index] = d
            count += 1
        tf.logging.info("{}".format(len(predictions)))
        predictions = list(filter(None, predictions))
        tf.logging.info("{}".format(len(predictions)))
        predictions = sorted(
            predictions, key=lambda x: x['prob'], reverse=True)

        return predictions
