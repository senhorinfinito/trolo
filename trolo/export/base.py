import os 
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '../..'))

import torch
import onnx
from abc import ABC, abstractmethod
from pathlib import Path
from typing  import Optional, List, Tuple, Union, Dict

from trolo.utils.smart_defaults import  infer_device

DEFAULT_EXPORT_FORMAT = ["onnx"]

class BaseExporter(ABC):
    def __init__(self, model_path : str, device : Optional[str] = None) -> None : 
                self.device =  torch.device(infer_device(device))
                self.model = self.load_model(model_path)
                self.model.to(self.device)
                self.model.eval()

    @abstractmethod
    def load_model(self, model_path: str) -> torch.nn.Module:
        """Load model from path"""
        pass
    
    @abstractmethod
    def export(
        self, 
        export_format : str = None,
        input_size : Union[int, List, Tuple, torch.Tensor] = None
    ) -> str:
        """
            Export the PyTorch model to ONNX format.
        """
        pass

    def _filter_format(self, export_format : str = DEFAULT_EXPORT_FORMAT) -> str:
        if export_format not in DEFAULT_EXPORT_FORMAT:
            raise ValueError(f"Unsupported export format: '{export_format}'. Supported formats are: {', '.join(DEFAULT_EXPORT_FORMAT)}")