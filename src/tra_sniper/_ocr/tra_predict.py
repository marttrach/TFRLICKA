"""Inference for tra_prediction_model.keras — prints one captcha per line.

The weights + charset ship next to this script (src/tra_sniper/_ocr/), so it
works the same on the training host and inside the API image -- the
THSR-Sniper pattern: weights in git, no venv ever crosses hosts.
"""
import json
import os
import sys

os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
import keras
import tensorflow as tf
from keras import layers

DATA = os.environ.get("TRA_OCR_DATA", os.path.dirname(os.path.abspath(__file__)))
# v3 (cleaned 472-sample trainset, golden 0.800) > v2 (golden 0.750) > v1.
if os.path.exists(f'{DATA}/tra_ocr_meta_v3.json'):
    META, PRED = 'tra_ocr_meta_v3.json', 'tra_prediction_model_v3.keras'
elif os.path.exists(f'{DATA}/tra_ocr_meta_v2.json'):
    META, PRED = 'tra_ocr_meta_v2.json', 'tra_prediction_model_v2.keras'
else:
    META, PRED = 'tra_ocr_meta.json', 'tra_prediction_model.keras'
with open(f'{DATA}/{META}') as fh:
    meta = json.load(fh)
W, H, MAXLEN = meta['img_width'], meta['img_height'], meta['max_length']
chars = meta['characters']

num_to_char = layers.StringLookup(
    vocabulary=chars, mask_token=None, invert=True)

model = keras.models.load_model(f'{DATA}/{PRED}', compile=False)

def predict(path):
    img = tf.io.read_file(path)
    img = tf.io.decode_image(img, channels=1, expand_animations=False)
    img = tf.image.convert_image_dtype(img, tf.float32)
    img = tf.image.resize(img, [H, W])
    img = (img - tf.math.reduce_mean(img)) / (tf.math.reduce_std(img) + 1e-6)
    img = tf.clip_by_value(img, -4, 4)
    img = tf.transpose(img, perm=[1, 0, 2])
    img = tf.expand_dims(img, 0)
    y = model.predict(img, verbose=0)
    ylog = tf.math.log(tf.transpose(y, [1, 0, 2]) + keras.backend.epsilon())
    decoded, _ = tf.nn.ctc_greedy_decoder(ylog, [y.shape[1]] * y.shape[0])
    st = tf.SparseTensor(decoded[0].indices, decoded[0].values, (y.shape[0], y.shape[1]))
    dense = tf.sparse.to_dense(st, default_value=-1)[0, :MAXLEN]
    return tf.strings.reduce_join(num_to_char(dense)).numpy().decode('utf-8')

for p in sys.argv[1:]:
    print(predict(p))
