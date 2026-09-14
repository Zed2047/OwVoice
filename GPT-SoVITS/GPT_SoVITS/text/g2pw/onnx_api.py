# This code is modified from https://github.com/PaddlePaddle/PaddleSpeech/tree/develop/paddlespeech/t2s/frontend/g2pw
# This code is modified from https://github.com/GitYCC/g2pW

import json
import hashlib
import os
import stat
import warnings
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import onnxruntime
import requests
from opencc import OpenCC
from pypinyin import Style, pinyin
from transformers.models.auto.tokenization_auto import AutoTokenizer

from ..zh_normalization.char_convert import tranditional_to_simplified
from .dataset import get_char_phoneme_labels, get_phoneme_labels, prepare_onnx_input
from .utils import load_config

onnxruntime.set_default_logger_severity(3)
try:
    # OwVoice 统一使用 CPU 版 ONNX Runtime；GPU 推理由 PyTorch 负责。
    onnxruntime.preload_dlls(cuda=False, cudnn=False)
except Exception:
    pass
warnings.filterwarnings("ignore")

model_version = "1.1"


def predict(session, onnx_input: Dict[str, Any], labels: List[str]) -> Tuple[List[str], List[float]]:
    all_preds = []
    all_confidences = []
    probs = session.run(
        [],
        {
            "input_ids": onnx_input["input_ids"],
            "token_type_ids": onnx_input["token_type_ids"],
            "attention_mask": onnx_input["attention_masks"],
            "phoneme_mask": onnx_input["phoneme_masks"],
            "char_ids": onnx_input["char_ids"],
            "position_ids": onnx_input["position_ids"],
        },
    )[0]

    preds = np.argmax(probs, axis=1).tolist()
    max_probs = []
    for index, arr in zip(preds, probs.tolist()):
        max_probs.append(arr[index])
    all_preds += [labels[pred] for pred in preds]
    all_confidences += max_probs

    return all_preds, all_confidences


def _load_json_from_candidates(filename: str, candidate_dirs: List[str]) -> Dict[str, Any]:
    for candidate_dir in candidate_dirs:
        if not candidate_dir:
            continue
        json_path = os.path.join(candidate_dir, filename)
        if os.path.exists(json_path):
            with open(json_path, "r", encoding="utf-8") as fr:
                return json.load(fr)
    raise FileNotFoundError(f"Cannot locate {filename} in candidate dirs: {candidate_dirs}")


def _find_first_existing_file(*paths: str) -> str:
    for path in paths:
        if path and os.path.exists(path):
            return path
    raise FileNotFoundError(f"Files not found: {paths}")


def download_and_decompress(model_dir: str = "G2PWModel/"):
    if not os.path.exists(model_dir):
        parent_directory = os.path.dirname(model_dir)
        zip_dir = os.path.join(parent_directory, "G2PWModel_1.1.zip")
        extract_dir = os.path.join(parent_directory, "G2PWModel_1.1")
        extract_dir_new = os.path.join(parent_directory, "G2PWModel")
        print("Downloading g2pw model...")
        lock_candidates = [Path.cwd() / "resource-lock.json", Path(__file__).resolve().parents[4] / "resource-lock.json"]
        lock_path = next((path for path in lock_candidates if path.is_file()), None)
        if lock_path is None:
            raise RuntimeError("缺少 resource-lock.json，拒绝从未固定地址下载 G2PW。请先运行 setup.bat。")
        try:
            g2pw_info = json.loads(lock_path.read_text(encoding="utf-8"))["g2pw"]
            modelscope_url = g2pw_info["url"]
            expected_size = int(g2pw_info["size_bytes"])
            expected_sha256 = str(g2pw_info["sha256"]).lower()
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"G2PW 资源锁无效：{lock_path}") from exc
        partial_path = zip_dir + ".part"
        with requests.get(modelscope_url, stream=True, timeout=(15, 120)) as r:
            r.raise_for_status()
            with open(partial_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        if os.path.getsize(partial_path) != expected_size:
            os.unlink(partial_path)
            raise RuntimeError("G2PW 下载大小校验失败")
        digest = hashlib.sha256()
        with open(partial_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest().lower() != expected_sha256:
            os.unlink(partial_path)
            raise RuntimeError("G2PW 下载 SHA256 校验失败")
        os.replace(partial_path, zip_dir)

        print("Extracting g2pw model...")
        destination = Path(parent_directory).resolve()
        with zipfile.ZipFile(zip_dir, "r") as zip_ref:
            for info in zip_ref.infolist():
                mode = (info.external_attr >> 16) & 0xFFFF
                if mode and stat.S_ISLNK(mode):
                    raise RuntimeError(f"G2PW 压缩包包含不安全链接：{info.filename}")
                output = (destination / info.filename).resolve()
                try:
                    output.relative_to(destination)
                except ValueError as exc:
                    raise RuntimeError(f"G2PW 压缩包包含不安全路径：{info.filename}") from exc
            zip_ref.extractall(destination)

        model_file = os.path.join(extract_dir, "g2pW.onnx")
        if not os.path.isfile(model_file) or os.path.getsize(model_file) != 635212732:
            raise RuntimeError("G2PW g2pW.onnx 大小校验失败")
        digest = hashlib.sha256()
        with open(model_file, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest().lower() != "2eb3c71fd95117b2e1abef8d2d0cd78aae894bbe7f0fac105ddc9c32ce63cbd0":
            raise RuntimeError("G2PW g2pW.onnx SHA256 校验失败")

        os.rename(extract_dir, extract_dir_new)

    return model_dir


class _G2PWBaseOnnxConverter:
    def __init__(
        self,
        model_dir: str = "G2PWModel/",
        style: str = "bopomofo",
        model_source: str = None,
        enable_non_tradional_chinese: bool = False,
    ):
        self.model_dir = download_and_decompress(model_dir)
        self.config = load_config(config_path=os.path.join(self.model_dir, "config.py"), use_default=True)

        self.model_source = model_source if model_source else self.config.model_source
        self.enable_opencc = enable_non_tradional_chinese
        self.tokenizer = AutoTokenizer.from_pretrained(self.model_source)

        polyphonic_chars_path = os.path.join(self.model_dir, "POLYPHONIC_CHARS.txt")
        monophonic_chars_path = os.path.join(self.model_dir, "MONOPHONIC_CHARS.txt")

        self.polyphonic_chars = [
            line.split("\t") for line in open(polyphonic_chars_path, encoding="utf-8").read().strip().split("\n")
        ]
        self.non_polyphonic = {
            "一",
            "不",
            "和",
            "咋",
            "嗲",
            "剖",
            "差",
            "攢",
            "倒",
            "難",
            "奔",
            "勁",
            "拗",
            "肖",
            "瘙",
            "誒",
            "泊",
            "听",
            "噢",
        }
        self.non_monophonic = {"似", "攢"}
        self.monophonic_chars = [
            line.split("\t") for line in open(monophonic_chars_path, encoding="utf-8").read().strip().split("\n")
        ]
        self.labels, self.char2phonemes = (
            get_char_phoneme_labels(polyphonic_chars=self.polyphonic_chars)
            if self.config.use_char_phoneme
            else get_phoneme_labels(polyphonic_chars=self.polyphonic_chars)
        )

        self.chars = sorted(list(self.char2phonemes.keys()))
        self.char2id = {char: idx for idx, char in enumerate(self.chars)}
        self.char_phoneme_masks = (
            {
                char: [1 if i in self.char2phonemes[char] else 0 for i in range(len(self.labels))]
                for char in self.char2phonemes
            }
            if self.config.use_mask
            else None
        )

        self.polyphonic_chars_new = set(self.chars)
        for char in self.non_polyphonic:
            self.polyphonic_chars_new.discard(char)

        self.monophonic_chars_dict = {char: phoneme for char, phoneme in self.monophonic_chars}
        for char in self.non_monophonic:
            self.monophonic_chars_dict.pop(char, None)

        default_asset_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "G2PWModel"))
        candidate_asset_dirs = [self.model_dir, default_asset_dir]
        self.bopomofo_convert_dict = _load_json_from_candidates(
            "bopomofo_to_pinyin_wo_tune_dict.json", candidate_asset_dirs
        )
        self.char_bopomofo_dict = _load_json_from_candidates("char_bopomofo_dict.json", candidate_asset_dirs)

        self.style_convert_func = {
            "bopomofo": lambda x: x,
            "pinyin": self._convert_bopomofo_to_pinyin,
        }[style]

        if self.enable_opencc:
            self.cc = OpenCC("s2tw")
        self.enable_sentence_dedup = os.getenv("g2pw_sentence_dedup", "true").strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
            "on",
        }
        # 聚焦到多音字附近上下文，默认左右各16字；设为0表示关闭裁剪（整句）。
        self.polyphonic_context_chars = max(0, int(os.getenv("g2pw_polyphonic_context_chars", "16")))

    def _convert_bopomofo_to_pinyin(self, bopomofo: str) -> str:
        tone = bopomofo[-1]
        assert tone in "12345"
        component = self.bopomofo_convert_dict.get(bopomofo[:-1])
        if component:
            return component + tone
        print(f'Warning: "{bopomofo}" cannot convert to pinyin')
        return None

    def __call__(self, sentences: List[str]) -> List[List[str]]:
        if isinstance(sentences, str):
            sentences = [sentences]

        if self.enable_opencc:
            translated_sentences = []
            for sent in sentences:
                translated_sent = self.cc.convert(sent)
                assert len(translated_sent) == len(sent)
                translated_sentences.append(translated_sent)
            sentences = translated_sentences

        texts, model_query_ids, result_query_ids, sent_ids, partial_results = self._prepare_data(sentences=sentences)
        if len(texts) == 0:
            return partial_results

        model_input = prepare_onnx_input(
            tokenizer=self.tokenizer,
            labels=self.labels,
            char2phonemes=self.char2phonemes,
            chars=self.chars,
            texts=texts,
            query_ids=model_query_ids,
            use_mask=self.config.use_mask,
            window_size=None,
            char2id=self.char2id,
            char_phoneme_masks=self.char_phoneme_masks,
        )

        if not model_input:
            return partial_results

        if self.enable_sentence_dedup:
            preds, _confidences = self._predict_with_sentence_dedup(model_input=model_input, texts=texts)
        else:
            preds, _confidences = self._predict(model_input=model_input)

        if self.config.use_char_phoneme:
            preds = [pred.split(" ")[1] for pred in preds]

        results = partial_results
        for sent_id, query_id, pred in zip(sent_ids, result_query_ids, preds):
            results[sent_id][query_id] = self.style_convert_func(pred)

        return results

    def _prepare_data(
        self, sentences: List[str]
    ) -> Tuple[List[str], List[int], List[int], List[int], List[List[str]]]:
        texts, model_query_ids, result_query_ids, sent_ids, partial_results = [], [], [], [], []
        for sent_id, sent in enumerate(sentences):
            sent_s = tranditional_to_simplified(sent)
            pypinyin_result = pinyin(sent_s, neutral_tone_with_five=True, style=Style.TONE3)
            partial_result = [None] * len(sent)
            polyphonic_indices: List[int] = []
            for i, char in enumerate(sent):
                if char in self.polyphonic_chars_new:
                    polyphonic_indices.append(i)
                elif char in self.monophonic_chars_dict:
                    partial_result[i] = self.style_convert_func(self.monophonic_chars_dict[char])
                elif char in self.char_bopomofo_dict:
                    partial_result[i] = pypinyin_result[i][0]
                else:
                    partial_result[i] = pypinyin_result[i][0]

            if polyphonic_indices:
                if self.polyphonic_context_chars > 0:
                    left = max(0, polyphonic_indices[0] - self.polyphonic_context_chars)
                    right = min(len(sent), polyphonic_indices[-1] + self.polyphonic_context_chars + 1)
                    sent_for_predict = sent[left:right]
                    query_offset = left
                else:
                    sent_for_predict = sent
                    query_offset = 0

                for index in polyphonic_indices:
                    texts.append(sent_for_predict)
                    model_query_ids.append(index - query_offset)
                    result_query_ids.append(index)
                    sent_ids.append(sent_id)

            partial_results.append(partial_result)
        return texts, model_query_ids, result_query_ids, sent_ids, partial_results

    def _predict(self, model_input: Dict[str, Any]) -> Tuple[List[str], List[float]]:
        raise NotImplementedError

    def _predict_with_sentence_dedup(
        self, model_input: Dict[str, Any], texts: List[str]
    ) -> Tuple[List[str], List[float]]:
        if len(texts) <= 1:
            return self._predict(model_input=model_input)

        grouped_indices: Dict[str, List[int]] = {}
        for idx, text in enumerate(texts):
            grouped_indices.setdefault(text, []).append(idx)

        if all(len(indices) == 1 for indices in grouped_indices.values()):
            return self._predict(model_input=model_input)

        preds: List[str] = [""] * len(texts)
        confidences: List[float] = [0.0] * len(texts)
        for indices in grouped_indices.values():
            group_input = {name: value[indices] for name, value in model_input.items()}
            if len(indices) > 1:
                for name in ("input_ids", "token_type_ids", "attention_masks"):
                    group_input[name] = group_input[name][:1]

            group_preds, group_confidences = self._predict(model_input=group_input)
            for output_idx, pred, confidence in zip(indices, group_preds, group_confidences):
                preds[output_idx] = pred
                confidences[output_idx] = confidence

        return preds, confidences


class G2PWOnnxConverter(_G2PWBaseOnnxConverter):
    def __init__(
        self,
        model_dir: str = "G2PWModel/",
        style: str = "bopomofo",
        model_source: str = None,
        enable_non_tradional_chinese: bool = False,
    ):
        super().__init__(
            model_dir=model_dir,
            style=style,
            model_source=model_source,
            enable_non_tradional_chinese=enable_non_tradional_chinese,
        )

        sess_options = onnxruntime.SessionOptions()
        sess_options.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess_options.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
        sess_options.intra_op_num_threads = 2

        onnx_path = _find_first_existing_file(
            os.path.join(self.model_dir, "g2pW.onnx"),
            os.path.join(self.model_dir, "g2pw.onnx"),
        )

        if "CUDAExecutionProvider" in onnxruntime.get_available_providers():
            self.session_g2pw = onnxruntime.InferenceSession(
                onnx_path,
                sess_options=sess_options,
                providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
            )
        else:
            self.session_g2pw = onnxruntime.InferenceSession(
                onnx_path,
                sess_options=sess_options,
                providers=["CPUExecutionProvider"],
            )

    def _predict(self, model_input: Dict[str, Any]) -> Tuple[List[str], List[float]]:
        return predict(session=self.session_g2pw, onnx_input=model_input, labels=self.labels)
