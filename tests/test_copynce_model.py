"""Model inference invariants, without downloads or private images."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
try:
    import numpy as np
    import torch
    import copynce_model as model
except ImportError:
    model = torch = np = None


@unittest.skipUnless(model is not None, "optional PyTorch runtime is unavailable")
class DescriptorTests(unittest.TestCase):
    def descriptor(self, device="cuda"):
        descriptor = model.Descriptor.__new__(model.Descriptor)
        descriptor.device = device
        descriptor.max_batch = None
        descriptor.fallbacks = []
        descriptor._release_accelerator = mock.Mock()
        descriptor._move = lambda device: setattr(descriptor, "device", str(device))
        return descriptor

    @staticmethod
    def vectors(images):
        return np.broadcast_to(np.asarray(images, dtype=np.float32)[:, None], (len(images), 512)).copy()

    def test_oom_reduction_keeps_every_image_in_order(self):
        descriptor = self.descriptor()
        attempted = []

        def forward(images):
            attempted.append(len(images))
            if len(images) > 2:
                raise RuntimeError("CUDA out of memory")
            return self.vectors(images)

        descriptor._forward = forward
        output = descriptor.encode(list(range(9)))
        np.testing.assert_array_equal(output[:, 0], np.arange(9))
        self.assertEqual(attempted[:3], [9, 4, 2])
        self.assertEqual(descriptor.device, "cuda")
        self.assertEqual(descriptor.max_batch, 2)
        self.assertGreaterEqual(descriptor._release_accelerator.call_count, 2)

    def test_single_image_oom_falls_back_to_cpu_without_loss(self):
        descriptor = self.descriptor("mps")

        def forward(images):
            if descriptor.device != "cpu":
                raise RuntimeError("MPS backend out of memory")
            return self.vectors(images)

        descriptor._forward = forward
        output = descriptor.encode([3, 7, 11])
        np.testing.assert_array_equal(output[:, 0], [3, 7, 11])
        self.assertEqual(descriptor.device, "cpu")
        self.assertIn("using CPU", descriptor.fallbacks[-1])

    def test_cpu_failure_propagates_instead_of_retrying_forever(self):
        descriptor = self.descriptor("cpu")
        descriptor._forward = mock.Mock(side_effect=RuntimeError("fixture failure"))
        with self.assertRaisesRegex(RuntimeError, "fixture failure"):
            descriptor.encode([1])
        descriptor._forward.assert_called_once()

    def test_nonfinite_features_are_rejected(self):
        descriptor = self.descriptor("cpu")
        descriptor._forward = lambda images: np.full((len(images), 512), np.nan, dtype=np.float32)
        with self.assertRaisesRegex(ValueError, "invalid feature"):
            descriptor.encode([1])

    def test_cpu_uses_fp32_even_inside_ambient_autocast(self):
        descriptor = self.descriptor("cpu")
        descriptor._mean = torch.zeros((1, 3, 1, 1))
        descriptor._std = torch.ones((1, 3, 1, 1))

        class TinyNetwork(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.projection = torch.nn.Linear(3, 512)

            def forward(self, batch):
                vector = self.projection(batch.mean(dim=(2, 3)))
                if vector.dtype != torch.float32:
                    raise AssertionError("FP32 pipeline changed precision")
                return vector

        descriptor._net = TinyNetwork().eval()
        descriptor._forward = model.Descriptor._forward.__get__(descriptor)
        image = np.zeros((224, 224, 3), dtype=np.uint8)
        with torch.autocast(device_type="cpu", dtype=torch.bfloat16):
            output = descriptor.encode([image])
        self.assertEqual(output.dtype, np.float32)

    def test_incorrect_checkpoint_is_rejected_before_deserialization(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "synthetic.pth"
            checkpoint.write_bytes(b"not a checkpoint")
            with mock.patch.object(model.torch, "load", side_effect=AssertionError):
                with self.assertRaisesRegex(ValueError, "size mismatch"):
                    model.Descriptor(str(checkpoint), device="cpu")


if __name__ == "__main__":
    unittest.main()
