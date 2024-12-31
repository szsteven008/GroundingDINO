import argparse
import os
import torch
import cv2
import numpy as np
from PIL import Image
from typing import Tuple, List
from torchvision.ops import box_convert
import onnx
import onnxruntime as ort

from groundingdino.util.inference import load_model, annotate
import groundingdino.datasets.transforms as T
from groundingdino.util.utils import get_phrases_from_posmap

class Model(torch.nn.Module):
    def __init__(
        self,
        model_config_path: str,
        model_checkpoint_path: str,
        device: str = "cuda"
    ):
        super().__init__()
        self.model = load_model(
            model_config_path=model_config_path,
            model_checkpoint_path=model_checkpoint_path,
            device=device
        ).to(device)
        self.tokenizer = self.model.tokenizer

#    def forward(self, samples: NestedTensor, targets: List = None, **kw):
    def forward(self, 
                image: torch.Tensor, 
                input_ids: torch.Tensor, 
                box_threshold: torch.Tensor, 
                text_threshold: torch.Tensor,
                **kw):
        token_type_ids = torch.zeros(input_ids.size(), dtype=torch.int32)
        attention_mask = (input_ids != 0).int()
        
        outputs = self.model(image, input_ids, attention_mask, token_type_ids)
        prediction_logits = outputs["pred_logits"].sigmoid().squeeze(0)
        prediction_boxes = outputs["pred_boxes"].squeeze(0)

        mask = prediction_logits.max(dim=1)[0] > box_threshold
        prediction_logits = prediction_logits[mask]
        prediction_input_ids_mask = prediction_logits > text_threshold
        prediction_boxes = prediction_boxes[mask]

        return prediction_logits.max(dim=1)[0].unsqueeze(0), prediction_boxes.unsqueeze(0), prediction_input_ids_mask.unsqueeze(0)

def preprocess_image(image_bgr: np.ndarray) -> torch.Tensor:
    image_bgr = cv2.resize(image_bgr, (800, 800))
    transform = T.Compose(
        [
            T.RandomResize([800], max_size=1333),
            T.ToTensor(),
            T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ]
    )
    image_pillow = Image.fromarray(cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB))
    image_transformed, _ = transform(image_pillow, None)
    return image_transformed

def preprocess_caption(caption: str) -> str:
    result = caption.lower().strip()
    if result.endswith("."):
        return result
    return result + "."

def export_onnx(model, output_dir):
    onnx_file = output_dir + "/" + "gdino.onnx"
    caption = preprocess_caption("watermark")
    tokenized = model.tokenizer(caption, padding="longest", return_tensors="pt")
    box_threshold = torch.tensor(0.35, dtype=torch.float32)
    text_threshold = torch.tensor(0.25, dtype=torch.float32)

    torch.onnx.export(
        model,
        args = (
            torch.rand(1, 3, 800, 800).type(torch.float32).to("cpu"), 
            tokenized["input_ids"], 
            box_threshold, 
            text_threshold),
        f = onnx_file,
        input_names = [ "image", "input_ids", "box_threshold", "text_threshold" ],
        output_names = [ "logits", "boxes", "masks" ], 
        opset_version = 17, 
        export_params = True, 
        do_constant_folding = True,
        dynamic_axes = {
            "input_ids": { 1: "token_num" }
        },
    )

    print("export onnx ok!")

    onnx_model = onnx.load(onnx_file)
    onnx.checker.check_model(onnx_model)
    print("check model ok!")

def inference(model):
    image = cv2.imread('asset/cat_dog.jpeg')
    processed_image = preprocess_image(image).unsqueeze(0)
    caption = preprocess_caption("cat . dog")
    tokenized = model.tokenizer(caption, padding="longest", return_tensors="pt")
    box_threshold = torch.tensor(0.35, dtype=torch.float32)
    text_threshold = torch.tensor(0.25, dtype=torch.float32)

    outputs = model(processed_image, 
                    tokenized["input_ids"], 
                    box_threshold, 
                    text_threshold)

    prediction_logits = outputs[0] 
    prediction_boxes = outputs[1] 
    prediction_masks = outputs[2] 

    input_ids = tokenized["input_ids"][0].tolist()
    phrases = []
    for mask in prediction_masks[0]:
        prediction_token_ids = [input_ids[i] for i in mask.nonzero(as_tuple=True)[0].tolist()]
        phrases.append(model.tokenizer.decode(prediction_token_ids).replace('.', ''))

    with torch.no_grad():
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        image = annotate(image, prediction_boxes[0], prediction_logits[0], phrases)

    cv2.imshow("image", image)
    cv2.waitKey()
    cv2.destroyAllWindows()

def inference_onnx(output_dir):
    onnx_file = output_dir + "/" + "gdino.onnx"
    session = ort.InferenceSession(onnx_file)

    image = cv2.imread('asset/cat_dog.jpeg')
    processed_image = preprocess_image(image).unsqueeze(0)
    caption = preprocess_caption("dog.cat")
    tokenized = model.tokenizer(caption, padding="longest", return_tensors="pt")
    box_threshold = torch.tensor(0.35, dtype=torch.float32)
    text_threshold = torch.tensor(0.25, dtype=torch.float32)

    outputs = session.run(None, {
        "image": processed_image.numpy().astype(np.float32) ,
        "input_ids": tokenized["input_ids"].numpy().astype(np.int64) , 
        "box_threshold": box_threshold.numpy().astype(np.float32) ,
        "text_threshold": text_threshold.numpy().astype(np.float32) 
    })

    prediction_logits = torch.from_numpy(outputs[0]) 
    prediction_boxes = torch.from_numpy(outputs[1]) 
    prediction_masks = torch.from_numpy(outputs[2]) 

    input_ids = tokenized["input_ids"][0].tolist()
    phrases = []
    for mask in prediction_masks[0]:
        prediction_token_ids = [input_ids[i] for i in mask.nonzero(as_tuple=True)[0].tolist()]
        phrases.append(model.tokenizer.decode(prediction_token_ids).replace('.', ''))

    with torch.no_grad():
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        image = annotate(image, prediction_boxes[0], prediction_logits[0], phrases)

    cv2.imshow("image", image)
    cv2.waitKey()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    parser = argparse.ArgumentParser("Export Grounding DINO Model to IR", add_help=True)
    parser.add_argument("--test", "-t", help="test onnx model", action="store_true")
    parser.add_argument("--orig", "-n", help="test model", action="store_true")
    parser.add_argument("--config_file", "-c", type=str, required=True, help="path to config file")
    parser.add_argument(
        "--checkpoint_path", "-p", type=str, required=True, help="path to checkpoint file"
    )
    parser.add_argument(
        "--output_dir", "-o", type=str, default="outputs", required=True, help="output directory"
    )

    args = parser.parse_args()

    # cfg
    config_file = args.config_file  # change the path of the model config file
    checkpoint_path = args.checkpoint_path  # change the path of the model
    output_dir = args.output_dir
    
    # make dir
    os.makedirs(output_dir, exist_ok=True)

    model = Model(config_file, checkpoint_path, device='cpu')

    if args.test:
        inference_onnx(output_dir)
    elif args.orig:
        inference(model)
    else:
        export_onnx(model, output_dir)
