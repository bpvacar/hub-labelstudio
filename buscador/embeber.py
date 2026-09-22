"""Recortes de cajas y embeddings con open_clip, para el buscador por especie.

Un recorte es un cuadrado centrado en la caja, con margen, guardado a LADO px.
Cuadrado a propósito: el preprocess de CLIP recorta al centro, y una caja
alargada (un tapir de perfil) perdería la cabeza o la cola.
"""
from __future__ import annotations

import gc
from dataclasses import dataclass

import numpy as np
import open_clip
import torch
import torch.nn.functional as F
from PIL import Image
from torch.utils.data import DataLoader, Dataset

LADO = 448       # px del recorte guardado: mayor que la entrada más grande (384)
MARGEN = 0.15    # contexto alrededor de la caja, por lado
MIN_LADO = 16    # px; una caja degenerada no rompe el recorte

# nombre corto → (arquitectura de open_clip, pesos)
MODELOS = {
    "bioclip-2": ("hf-hub:imageomics/bioclip-2", None),
    "siglip-so400m-384": ("ViT-SO400M-14-SigLIP-384", "webli"),   # el de INQUIRE-Search
}


def cuadrado(x1, y1, x2, y2, ancho, alto, margen=MARGEN):
    """Caja [x1 y1 x2 y2] → cuadrado con margen, desplazado para caber en la foto."""
    lado = max(x2 - x1, y2 - y1, MIN_LADO) * (1 + 2 * margen)
    lado = min(lado, ancho, alto)
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    izq = min(max(cx - lado / 2, 0), ancho - lado)
    arriba = min(max(cy - lado / 2, 0), alto - lado)
    return round(izq), round(arriba), round(izq + lado), round(arriba + lado)


@dataclass
class Modelo:
    nombre: str
    red: torch.nn.Module
    preprocess: object
    tokenizer: object
    device: str


def cargar(nombre: str, device: str = "cuda") -> Modelo:
    """En CPU tarda ~20 s en cargar y sirve para consultas de texto sueltas."""
    arquitectura, pesos = MODELOS[nombre]
    red, _, preprocess = open_clip.create_model_and_transforms(arquitectura, pretrained=pesos)
    return Modelo(nombre, red.to(device).eval(), preprocess,
                  open_clip.get_tokenizer(arquitectura), device)


class _Archivos(Dataset):
    def __init__(self, rutas, preprocess):
        self.rutas, self.preprocess = rutas, preprocess

    def __len__(self):
        return len(self.rutas)

    def __getitem__(self, i):
        with Image.open(self.rutas[i]) as im:
            return self.preprocess(im.convert("RGB"))


@torch.no_grad()
def imagenes(m: Modelo, rutas: list[str], lote: int = 64, workers: int = 8) -> np.ndarray:
    """Embeddings L2-normalizados, float32, en el orden de `rutas`."""
    salida = []
    for x in DataLoader(_Archivos(rutas, m.preprocess), batch_size=lote,
                        num_workers=workers, pin_memory=m.device == "cuda"):
        with torch.autocast(m.device, dtype=torch.float16, enabled=m.device == "cuda"):
            e = m.red.encode_image(x.to(m.device, non_blocking=True))
        salida.append(F.normalize(e.float(), dim=-1).cpu())
    return torch.cat(salida).numpy()


@torch.no_grad()
def texto(m: Modelo, frases: list[str]) -> np.ndarray:
    """Un solo vector para varias frases: la media normalizada (prompt ensemble)."""
    with torch.autocast(m.device, dtype=torch.float16, enabled=m.device == "cuda"):
        e = m.red.encode_text(m.tokenizer(frases).to(m.device))
    v = F.normalize(e.float(), dim=-1).mean(0)
    return F.normalize(v, dim=0).cpu().numpy()


def liberar(m: Modelo) -> None:
    """Suelta la GPU antes de cargar el siguiente modelo: la GPU es compartida."""
    del m.red
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
