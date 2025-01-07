import os
import json
import argparse

import cv2
import numpy as np

import torch
from torchmetrics.detection.mean_ap import MeanAveragePrecision
import lightning as L
from lightning.pytorch.callbacks import ModelCheckpoint, EarlyStopping
from lightning.pytorch.loggers import WandbLogger

from src.dataset import COCODataModule
from src.model import create_model
from src.utils import SEED


L.seed_everything(SEED)

class SegmentationModel(L.LightningModule):
    def __init__(self, model, batch_size: int = 8):
        super().__init__()
        self.model = model
        self.batch_size = batch_size

        self.val_map = MeanAveragePrecision()
        self.best_val_map = 0
        self.test_map = MeanAveragePrecision()

    def forward(self, inputs):
        return self.model(inputs)
    
    def training_step(self, batch, batch_idx):
        inputs, targets = batch
        inputs = [input.to(self.device) for input in inputs]
        targets = [{k: v.to(self.device) for k, v in t.items()} for t in targets]

        outputs = self.model(inputs, targets)

        cls_loss = outputs.get('loss_classifier', 0)
        box_loss = outputs.get('loss_box_reg', 0)
        mask_loss = outputs.get('loss_mask', 0)

        loss = cls_loss + box_loss + mask_loss

        self.log('train-total_loss', loss)
        self.log('train-cls_loss', cls_loss)
        self.log('train-box_loss', box_loss)
        self.log('train-mask_loss', mask_loss)
        
        return loss

    def validation_step(self, batch, batch_idx):
        inputs, targets = batch
        inputs = [input.to(self.device) for input in inputs]
        targets = [{k: v.to(self.device) for k, v in t.items()} for t in targets]

        outputs = self.model(inputs)
        self.val_map.update(outputs, targets)
        
        return outputs

    def on_validation_epoch_end(self):
        val_mAP = self.val_map.compute()
        self.val_map.reset()

        if self.best_val_map < val_mAP['map'].item():
            self.best_val_map = val_mAP['map'].item()

        self.log('val_mAP', val_mAP['map'].item()) 
        self.log('best_val_mAP', self.best_val_map) 

    def test_step(self, batch, batch_idx):
        inputs, targets = batch
        inputs = [input.to(self.device) for input in inputs]
        targets = [{k: v.to(self.device) for k, v in t.items()} for t in targets] 

        outputs = self.model(inputs, targets)
        self.test_map.update(outputs, targets)
        
        return outputs
    
    def on_test_epoch_end(self):
        test_mAP = self.test_map.compute()
        self.test_map.reset()

        self.log('test_mAP', test_mAP['map'].item())

    def predict_step(self, batch, batch_idx):
        inputs, img = batch
        outputs = self.model(inputs)
        
        if len(outputs) > 0:
            pred_boxes = outputs[0]['boxes']
            pred_labels = outputs[0]['labels']
            pred_scores = outputs[0]['scores']
            pred_masks = outputs[0]['masks']
            
            score_threshold = 0.7
            mask_threshold = 0.7
            
            high_conf_idx = pred_scores > score_threshold
            boxes = pred_boxes[high_conf_idx].cpu().numpy()
            labels = pred_labels[high_conf_idx].cpu().numpy()
            scores = pred_scores[high_conf_idx].cpu().numpy()
            masks = pred_masks[high_conf_idx] > mask_threshold
            masks = masks.squeeze(1).cpu().numpy()
            
            return {
                'boxes': boxes,
                'labels': labels,
                'scores': scores,
                'masks': masks
            }, img
        
        return None, img
    
    def configure_optimizers(self):
        return torch.optim.SGD(self.model.parameters(), lr=1e-3, momentum=0.9)


def main(segmentaion_model, data, batch, epoch, device, save_path, gpus, precision, mode, ckpt):
    if not os.path.exists(save_path):
        os.makedirs(save_path)

    model = SegmentationModel(create_model(segmentaion_model))

    if device == 'gpu':
        if len(gpus) == 1:
            gpus = [int(gpus)]
        else:
            gpus = list(map(int, gpus.split(',')))

    elif device == 'cpu':
        gpus = 'auto'
        precision = 32

    checkpoint_callback = ModelCheckpoint(
        monitor = 'val_mAP',
        mode = 'max',
        dirpath= f'{save_path}',
        filename= f'{segmentaion_model}-'+'{epoch:02d}-{val_mAP:.2f}',
        save_top_k = 1
    )
    early_stopping = EarlyStopping(
        monitor = 'val_mAP',
        mode = 'max',
        patience=5
    )
    wandb_logger = WandbLogger(project="COCO-instance-segmentation")

    if mode == 'train':
        trainer = L.Trainer(
            accelerator=device,
            devices=gpus,
            max_epochs=epoch,
            precision=precision,
            logger = wandb_logger,
            callbacks=[checkpoint_callback, early_stopping],
        )
        
        trainer.fit(model, COCODataModule(data=data, batch_size=batch))
        trainer.test(model, COCODataModule(data=data, batch_size=batch))

    else:
        trainer = L.Trainer(
            accelerator=device,
            devices=gpus,
            precision=precision
        )
        model = SegmentationModel.load_from_checkpoint(ckpt, model=create_model(segmentaion_model))
        predictions, img = trainer.predict(model, COCODataModule(data=data, mode='predict'))[0]

        with open(os.path.join('../dataset/instances_val2017.json'), 'r') as f:
            coco_data = json.load(f)
            categories = coco_data['categories']

        if predictions is not None:
            pred_boxes = predictions['boxes']
            pred_labels = predictions['labels']
            pred_scores = predictions['scores']
            pred_masks = predictions['masks']
            
            score_threshold = 0.7
            mask_threshold = 0.7

            high_conf_idx = pred_scores > score_threshold
            boxes = pred_boxes[high_conf_idx]
            labels = pred_labels[high_conf_idx]
            scores = pred_scores[high_conf_idx]
            masks = pred_masks[high_conf_idx] > mask_threshold

            img = cv2.imread(data)
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            
            for box, label, score, mask in zip(boxes, labels, scores, masks):
                color = np.random.randint(0, 255, 3).tolist()
                img_mask = img.copy()
                img_mask[mask] = img_mask[mask] * 0.5 + np.array(color) * 0.5
                
                for category in categories:
                    if category['id'] == label:
                        pred_cate = category['name']
                label_text = f'Class {pred_cate}: {score:.2f}'
                cv2.putText(
                    img_mask,
                    label_text,
                    (int(box[0]), int(box[1]) - 10),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    color,
                    1
                )
                img = img_mask

            cv2.imshow('Instance Segmentation Result', img)
            cv2.waitKey(0) 
            cv2.destroyAllWindows()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('-m', '--model', type=str, default='mask_rcnn')
    parser.add_argument('-b', '--batch_size', dest='batch', type=int, default=8)
    parser.add_argument('-e', '--epoch', type=int, default=10)
    parser.add_argument('-d', '--data_path', dest='data', type=str, default='../dataset')
    parser.add_argument('-s', '--save_path', dest='save', type=str, default='./checkpoint/')
    parser.add_argument('-dc', '--device', type=str, default='gpu')
    parser.add_argument('-g', '--gpus', type=str, nargs='+', default='0')
    parser.add_argument('-p', '--precision', type=str, default='32-true')
    parser.add_argument('-mo', '--mode', type=str, default='train')
    parser.add_argument('-c', '--ckpt_path', dest='ckpt', type=str, default='./checkpoint/')
    args = parser.parse_args()
    
    main(args.model, args.data, args.batch, args.epoch, args.device, args.save, 
         args.gpus, args.precision, args.mode, args.ckpt)