import argparse
import os
import copy
import re

import numpy as np
import json
import torch
import torchvision
from PIL import Image, ImageDraw, ImageFont
from tqdm import tqdm
import sys

import nltk
nltk.download(['punkt', 'averaged_perceptron_tagger', 'wordnet', 'omw-1.4'])


# Grounding DINO
import GroundingDINO.groundingdino.datasets.transforms as T
from GroundingDINO.groundingdino.models import build_model
from GroundingDINO.groundingdino.util import box_ops
from GroundingDINO.groundingdino.util.slconfig import SLConfig
from GroundingDINO.groundingdino.util.utils import clean_state_dict, get_phrases_from_posmap

# segment anything
# from segment_anything import build_sam, SamPredictor 
import cv2
import numpy as np
import matplotlib.pyplot as plt

# Tag2Text
# sys.path.append('Tag2Text')
# from Tag2Text.models import tag2text
# from Tag2Text import inference
# import torchvision.transforms as TS

# # BLIP
from transformers import BlipProcessor, BlipForConditionalGeneration

# ChatGPT
# import openai

import warnings
warnings.filterwarnings("ignore")

def load_image(image_path):
    # load image
    image_pil = Image.open(image_path).convert("RGB")  # load image

    transform = T.Compose(
        [
            T.RandomResize([800], max_size=1333),
            T.ToTensor(),
            T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ]
    )
    image, _ = transform(image_pil, None)  # 3, h, w
    return image_pil, image


def generate_caption(raw_image, device, processor, blip_model):
    # unconditional image captioning
    if device == "cuda":
        inputs = processor(raw_image, return_tensors="pt").to("cuda", torch.float16)
    else:
        inputs = processor(raw_image, return_tensors="pt")
    out = blip_model.generate(**inputs)
    caption = processor.decode(out[0], skip_special_tokens=True)
    return caption


def generate_tags(caption, split=',', max_tokens=100, model="gpt-3.5-turbo"):
    lemma = nltk.wordnet.WordNetLemmatizer()
    tags_list = [word for (word, pos) in nltk.pos_tag(nltk.word_tokenize(caption)) if pos[0] == 'N']
    tags_lemma = [lemma.lemmatize(w) for w in tags_list]
    tags = ', '.join(map(str, tags_lemma))
    return tags


def check_caption(caption, pred_phrases, max_tokens=100, model="gpt-3.5-turbo"):
    object_list = [obj.split('(')[0] for obj in pred_phrases]
    object_num = []
    for obj in set(object_list):
        object_num.append(f'{object_list.count(obj)} {obj}')
    object_num = ', '.join(object_num)
    # print(f"Correct object number: {object_num}")
    return caption


def load_model(model_config_path, model_checkpoint_path, device):
    args = SLConfig.fromfile(model_config_path)
    args.device = device
    model = build_model(args)
    checkpoint = torch.load(model_checkpoint_path, map_location="cpu")
    load_res = model.load_state_dict(clean_state_dict(checkpoint["model"]), strict=False)
    # print(load_res)
    _ = model.eval()
    return model


def get_grounding_output(model, image, caption, box_threshold, text_threshold,device="cpu"):
    caption = caption.lower()
    caption = caption.strip()
    if not caption.endswith("."):
        caption = caption + "."
    model = model.to(device)
    image = image.to(device)
    with torch.no_grad():
        outputs = model(image[None], captions=[caption])
    logits = outputs["pred_logits"].cpu().sigmoid()[0]  # (nq, 256)
    boxes = outputs["pred_boxes"].cpu()[0]  # (nq, 4)
    logits.shape[0]

    # filter output
    logits_filt = logits.clone()
    boxes_filt = boxes.clone()
    filt_mask = logits_filt.max(dim=1)[0] > box_threshold
    logits_filt = logits_filt[filt_mask]  # num_filt, 256
    boxes_filt = boxes_filt[filt_mask]  # num_filt, 4
    logits_filt.shape[0]

    # get phrase
    tokenlizer = model.tokenizer
    tokenized = tokenlizer(caption)
    # build pred
    pred_phrases = []
    scores = []
    # tags for object identification
    tags = []
    for logit, box in zip(logits_filt, boxes_filt):
        pred_phrase = get_phrases_from_posmap(logit > text_threshold, tokenized, tokenlizer)
        objects = pred_phrase.split()
        tags.append(objects)
        pred_phrases.append(pred_phrase + f"({str(logit.max().item())[:4]})")
        scores.append(logit.max().item())

    return boxes_filt, torch.Tensor(scores), pred_phrases, tags


def show_mask(mask, ax, random_color=False):
    if random_color:
        color = np.concatenate([np.random.random(3), np.array([0.6])], axis=0)
    else:
        color = np.array([30/255, 144/255, 255/255, 0.6])
    h, w = mask.shape[-2:]
    mask_image = mask.reshape(h, w, 1) * color.reshape(1, 1, -1)
    ax.imshow(mask_image)


def show_box(box, ax, label):
    x0, y0 = box[0], box[1]
    w, h = box[2] - box[0], box[3] - box[1]
    ax.add_patch(plt.Rectangle((x0, y0), w, h, edgecolor='green', facecolor=(0,0,0,0), lw=2)) 
    ax.text(x0, y0, label)


def save_mask_data(output_dir, caption, mask_list, box_list, label_list):
    value = 0  # 0 for background

    mask_img = torch.zeros(mask_list.shape[-2:])
    for idx, mask in enumerate(mask_list):
        mask_img[mask.cpu().numpy()[0] == True] = value + idx + 1
    plt.figure(figsize=(10, 10))
    plt.imshow(mask_img.numpy())
    plt.axis('off')
    plt.savefig(os.path.join(output_dir, 'mask.jpg'), bbox_inches="tight", dpi=300, pad_inches=0.0)

    json_data = {
        'caption': caption,
        'mask':[{
            'value': value,
            'label': 'background'
        }]
    }
    for label, box in zip(label_list, box_list):
        value += 1
        name, logit = label.split('(')
        logit = logit[:-1] # the last is ')'
        json_data['mask'].append({
            'value': value,
            'label': name,
            'logit': float(logit),
            'box': box.numpy().tolist(),
        })
    with open(os.path.join(output_dir, 'label.json'), 'w') as f:
        json.dump(json_data, f)
    
def num_sort(input_string):
    return list(map(int, re.findall(r'\d+', input_string)))[0]

def parse_args():
    parser = argparse.ArgumentParser("Grounded-Segment-Anything Demo", add_help=True)
    parser.add_argument("--config", type=str, required=True, help="path to config file")
    parser.add_argument(
        "--grounded_checkpoint", type=str, required=True, help="path to checkpoint file"
    )
    parser.add_argument(
        "--tag2text_checkpoint", type=str, required=True, help="path to checkpoint file"
    )
    # parser.add_argument(
    #     "--sam_checkpoint", type=str, required=True, help="path to checkpoint file"
    # )
    # parser.add_argument("--input_image", type=str, required=True, help="path to image file")
    parser.add_argument("--split", default=",", type=str, help="split for text prompt")
    # parser.add_argument("--openai_key", type=str, help="key for chatgpt")
    # parser.add_argument("--openai_proxy", default=None, type=str, help="proxy for chatgpt")
    # parser.add_argument(
    #     "--output_dir", "-o", type=str, default="outputs", required=True, help="output directory"
    # )

    parser.add_argument("--box_threshold", type=float, default=0.25, help="box threshold")
    parser.add_argument("--text_threshold", type=float, default=0.2, help="text threshold")
    parser.add_argument("--iou_threshold", type=float, default=0.5, help="iou threshold")

    parser.add_argument("--device", type=str, default="cpu", help="running on cpu only!, default=False")

    parser.add_argument("--data-dir", type=str, default="./data", 
            help="Path for parent folder with images and depth info")

    args = parser.parse_args()
    return args

def automatic_label(args, image_dir=None):
    # cfg
    config_file = args.config  # change the path of the model config file
    grounded_checkpoint = args.grounded_checkpoint  # change the path of the model
    # sam_checkpoint = args.sam_checkpoint
    tag2text_checkpoint = args.tag2text_checkpoint 
    # image_path = args.input_image
    split = args.split
    # openai_key = args.openai_key
    # openai_proxy = args.openai_proxy
    # output_dir = args.output_dir
    box_threshold = args.box_threshold
    text_threshold = args.text_threshold
    iou_threshold = args.iou_threshold
    device = args.device

    # openai.api_key = openai_key
    # if openai_proxy:
    #     openai.proxy = {"http": openai_proxy, "https": openai_proxy}

    # load model
    model = load_model(config_file, grounded_checkpoint, device=device)
    # predictor = SamPredictor(build_sam(checkpoint=sam_checkpoint).to(device))

    # # visualize raw image
    # image_pil.save(os.path.join(output_dir, "raw_image.jpg"))

    # generate caption and tags
    # use Tag2Text can generate better captions
    # https://huggingface.co/spaces/xinyu1205/Tag2Text
    # but there are some bugs...
    processor = BlipProcessor.from_pretrained("Salesforce/blip-image-captioning-large")
    if device == "cuda":
        blip_model = BlipForConditionalGeneration.from_pretrained("Salesforce/blip-image-captioning-large", torch_dtype=torch.float16).to("cuda")
    else:
        blip_model = BlipForConditionalGeneration.from_pretrained("Salesforce/blip-image-captioning-large")


    # # initialize Tag2Text
    # normalize = TS.Normalize(mean=[0.485, 0.456, 0.406],
    #                                  std=[0.229, 0.224, 0.225])
    # transform = TS.Compose([
    #                 TS.Resize((384, 384)),
    #                 TS.ToTensor(), normalize
    #             ])
    
    # # filter out attributes and action categories which are difficult to grounding
    # delete_tag_index = []
    # for i in range(3012, 3429):
    #     delete_tag_index.append(i)

    # specified_tags='None'
    # # load model
    # tag2text_model = tag2text.tag2text_caption(pretrained=tag2text_checkpoint,
    #                                     image_size=384,
    #                                     vit='swin_b',
    #                                     delete_tag_index=delete_tag_index)
    # # threshold for tagging
    # # we reduce the threshold to obtain more tags
    # tag2text_model.threshold = 0.64 
    # tag2text_model.eval()

    if image_dir is None:
        data_dir = args.data_dir
        image_dir = os.path.join(data_dir, "crop")
    
    image_list = sorted(os.listdir(image_dir), key=num_sort)

    tag_freq = {}
    ignored_tags = ["someone", "person", "hand", "table", "robot", \
                    "desk", "room", "man", "arm", "boy", "woman", \
                    "machine", "counter", "pan", "paper", "box", "mouse"]
    for image_idx in tqdm(range(len(image_list))):
        image_num = image_list[image_idx]
        image_path = os.path.join(image_dir, image_num)
        # load image
        image_pil, image = load_image(image_path)

        # tag2text_model = tag2text_model.to(device)
        # raw_image = image_pil.resize(
        #                 (384, 384))
        # raw_image  = transform(raw_image).unsqueeze(0)

        # res = inference.inference(raw_image , tag2text_model, specified_tags)

        # # Currently ", " is better for detecting single tags
        # # while ". " is a little worse in some case
        # text_prompt=res[0].replace(' |', ',')
        # object_list = text_prompt.split()
        # tqdm.write("found objs: {}".format(object_list))
        # for obj in object_list:
        #     if obj not in ignored_tags:
        #         if obj in tag_freq:
        #             tag_freq[obj] += 1
        #         else:
        #             tag_freq[obj] = 1
        # print(text_prompt)
        # caption=res[2]

        caption = generate_caption(image_pil, device=device, processor=processor, blip_model=blip_model)
        # Currently ", " is better for detecting single tags
        # while ". " is a little worse in some case
        text_prompt = generate_tags(caption, split=split)
        # object_list = text_prompt.split()
        # tqdm.write("found objs: {}".format(object_list))
        # for obj in object_list:
        #     if obj not in ignored_tags:
        #         if obj in tag_freq:
        #             tag_freq[obj] += 1
        #         else:
        #             tag_freq[obj] = 1
        # print(f"Caption: {caption}")
        # print(f"Tags: {text_prompt}")

        # run grounding dino model
        boxes_filt, scores, pred_phrases, object_list = get_grounding_output(
            model, image, text_prompt, box_threshold, text_threshold, device=device
        )
        object_list = [item for sublist in object_list for item in sublist]
        # object_list = [obj.split('(')[0] for obj in pred_phrases]

        tqdm.write("found objs: {}".format(object_list))
        for obj in object_list:
            if obj not in ignored_tags:
                if obj in tag_freq:
                    tag_freq[obj] += 1
                else:
                    tag_freq[obj] = 1


    # print(tag_freq)
    return tag_freq


if __name__ == "__main__":

    args = parse_args()
    automatic_label(args)