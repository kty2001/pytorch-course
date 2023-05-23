import torch
from torch import nn
from torch.utils.data import Dataset

from src.model import LeNet


def predict(test_data: Dataset, model: nn.Module) -> None:
    """학습한 뉴럴 네트워크로 CIFAR-10 데이터셋을 분류합니다.

    :param test_data: 추론에 사용되는 데이터셋
    :type test_data: Dataset
    :param model: 추론에 사용되는 모델
    :type model: nn.Module
    """
    classes = [
        'plane',
        'car',
        'bird',
        'cat',
        'deer',
        'dog',
        'frog',
        'horse',
        'ship',
        'truck',
    ]

    model.eval()
    image = test_data[0][0].unsqueeze(0)
    target = test_data[0][1]
    with torch.no_grad():
        pred = model(image)
        predicted = classes[pred[0].argmax(0)]
        actual = classes[target]
        print(f'Predicted: "{predicted}", Actual: "{actual}"')


def test():
    num_classes = 10

    test_data = 

    model = LeNet(num_classes=num_classes)
    model.load_state_dict(torch.load('cifar-net-lenet.pth'))

    predict(test_data, model)