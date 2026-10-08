import ctypes
import gc
import json
import logging
import os

import numpy as np

from helpers.decider_src import prompt as _prompt
from helpers.decider_src import temperature as _TT

log = logging.getLogger("pocketflow-pipeline")

_LLAMA_SO = "/media/marcs/Linux_Apps/llama.cpp/build/bin/libllama.so"

_GGUF_DEFAULT = "/media/marcs/Linux_Apps/LLM/decider-4b/decider-4b-v2.1-Q4_K_M.gguf"
_N_CTX_DEFAULT = 8192
_N_GPU_LAYERS = -1
_MAX_CTX_TOKENS = 1536


class _LlamaModelParams(ctypes.Structure):
    _fields_ = [
        ("devices", ctypes.c_void_p),
        ("tensor_buft_overrides", ctypes.c_void_p),
        ("n_gpu_layers", ctypes.c_int32),
        ("split_mode", ctypes.c_int32),
        ("main_gpu", ctypes.c_int32),
        ("tensor_split", ctypes.POINTER(ctypes.c_float)),
        ("progress_callback", ctypes.c_void_p),
        ("progress_callback_user_data", ctypes.c_void_p),
        ("kv_overrides", ctypes.c_void_p),
        ("vocab_only", ctypes.c_bool),
        ("use_mmap", ctypes.c_bool),
        ("use_direct_io", ctypes.c_bool),
        ("use_mlock", ctypes.c_bool),
        ("check_tensors", ctypes.c_bool),
        ("use_extra_bufts", ctypes.c_bool),
        ("no_host", ctypes.c_bool),
        ("no_alloc", ctypes.c_bool),
    ]


class _LlamaContextParams(ctypes.Structure):
    _fields_ = [
        ("n_ctx", ctypes.c_uint32),
        ("n_batch", ctypes.c_uint32),
        ("n_ubatch", ctypes.c_uint32),
        ("n_seq_max", ctypes.c_uint32),
        ("n_threads", ctypes.c_int32),
        ("n_threads_batch", ctypes.c_int32),
        ("rope_scaling_type", ctypes.c_int32),
        ("pooling_type", ctypes.c_int32),
        ("attention_type", ctypes.c_int32),
        ("flash_attn_type", ctypes.c_int32),
        ("rope_freq_base", ctypes.c_float),
        ("rope_freq_scale", ctypes.c_float),
        ("yarn_ext_factor", ctypes.c_float),
        ("yarn_attn_factor", ctypes.c_float),
        ("yarn_beta_fast", ctypes.c_float),
        ("yarn_beta_slow", ctypes.c_float),
        ("yarn_orig_ctx", ctypes.c_uint32),
        ("defrag_thold", ctypes.c_float),
        ("cb_eval", ctypes.c_void_p),
        ("cb_eval_user_data", ctypes.c_void_p),
        ("type_k", ctypes.c_int32),
        ("type_v", ctypes.c_int32),
        ("abort_callback", ctypes.c_void_p),
        ("abort_callback_data", ctypes.c_void_p),
        ("embeddings", ctypes.c_bool),
        ("offload_kqv", ctypes.c_bool),
        ("no_perf", ctypes.c_bool),
        ("op_offload", ctypes.c_bool),
        ("swa_full", ctypes.c_bool),
        ("kv_unified", ctypes.c_bool),
        ("samplers", ctypes.c_void_p),
        ("n_samplers", ctypes.c_size_t),
    ]


class _LlamaBatch(ctypes.Structure):
    _fields_ = [
        ("n_tokens", ctypes.c_int32),
        ("token", ctypes.POINTER(ctypes.c_int32)),
        ("embd", ctypes.POINTER(ctypes.c_float)),
        ("pos", ctypes.POINTER(ctypes.c_int32)),
        ("n_seq_id", ctypes.POINTER(ctypes.c_int32)),
        ("seq_id", ctypes.POINTER(ctypes.POINTER(ctypes.c_int32))),
        ("logits", ctypes.POINTER(ctypes.c_int8)),
    ]


_LOGCALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_int32, ctypes.c_char_p, ctypes.c_void_p)


def _load_lib():
    lib = ctypes.CDLL(os.path.realpath(_LLAMA_SO) if os.path.exists(os.path.realpath(_LLAMA_SO)) else _LLAMA_SO)

    lib.llama_backend_init.restype = None
    lib.llama_backend_free.restype = None
    lib.llama_log_set.restype = None
    lib.llama_log_set.argtypes = [_LOGCALLBACK, ctypes.c_void_p]

    lib.llama_model_default_params.restype = _LlamaModelParams
    lib.llama_model_load_from_file.restype = ctypes.c_void_p
    lib.llama_model_load_from_file.argtypes = [ctypes.c_char_p, _LlamaModelParams]

    lib.llama_context_default_params.restype = _LlamaContextParams
    lib.llama_init_from_model.restype = ctypes.c_void_p
    lib.llama_init_from_model.argtypes = [ctypes.c_void_p, _LlamaContextParams]

    lib.llama_model_get_vocab.restype = ctypes.c_void_p
    lib.llama_model_get_vocab.argtypes = [ctypes.c_void_p]
    lib.llama_vocab_n_tokens.restype = ctypes.c_int32
    lib.llama_vocab_n_tokens.argtypes = [ctypes.c_void_p]
    lib.llama_vocab_get_text.restype = ctypes.c_char_p
    lib.llama_vocab_get_text.argtypes = [ctypes.c_void_p, ctypes.c_int32]

    lib.llama_tokenize.restype = ctypes.c_int32
    lib.llama_tokenize.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32,
        ctypes.POINTER(ctypes.c_int32), ctypes.c_int32,
        ctypes.c_bool, ctypes.c_bool,
    ]

    lib.llama_batch_init.restype = _LlamaBatch
    lib.llama_batch_init.argtypes = [ctypes.c_int32, ctypes.c_int32, ctypes.c_int32]
    lib.llama_batch_free.restype = None
    lib.llama_batch_free.argtypes = [_LlamaBatch]

    lib.llama_decode.restype = ctypes.c_int32
    lib.llama_decode.argtypes = [ctypes.c_void_p, _LlamaBatch]

    lib.llama_get_logits_ith.restype = ctypes.POINTER(ctypes.c_float)
    lib.llama_get_logits_ith.argtypes = [ctypes.c_void_p, ctypes.c_int32]

    lib.llama_get_memory.restype = ctypes.c_void_p
    lib.llama_get_memory.argtypes = [ctypes.c_void_p]
    lib.llama_memory_clear.restype = None
    lib.llama_memory_clear.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    lib.llama_n_ctx.restype = ctypes.c_uint32
    lib.llama_n_ctx.argtypes = [ctypes.c_void_p]

    lib.llama_free_model.restype = None
    lib.llama_free_model.argtypes = [ctypes.c_void_p]
    lib.llama_free.restype = None
    lib.llama_free.argtypes = [ctypes.c_void_p]
    return lib


class _Tokenizer:
    """Mini tokenizer shim compatible avec decider.prompt (encode('...', add_special_tokens=False)).
    Tokenise via llama_tokenize sur le vocab du modèle chargé : identique au tokenizer HF du GGUF."""

    def __init__(self, lib, vocab):
        self._lib = lib
        self._vocab = vocab

    def encode(self, text, add_special_tokens=False, parse_special=False):
        """Quirk de ce build llama.cpp : llama_tokenize renvoie la taille requise
        en NÉGATIF quand le buffer est trop petit (-1 = 1 token, ...)."""
        raw = str(text).encode("utf-8")
        n = self._lib.llama_tokenize(self._vocab, raw, len(raw), None, 0, add_special_tokens, parse_special)
        if n == 0:
            return []
        size = -n if n < 0 else n
        if size <= 0:
            return []
        buf = (ctypes.c_int32 * size)()
        got = self._lib.llama_tokenize(self._vocab, raw, len(raw), buf, size, add_special_tokens, parse_special)
        if got < 0:
            size2 = -got
            buf = (ctypes.c_int32 * size2)()
            got = self._lib.llama_tokenize(self._vocab, raw, len(raw), buf, size2, add_special_tokens, parse_special)
        if got <= 0:
            return []
        return list(buf)[:got]

    def decode(self, ids, remove_special_tokens=True, unparse_special=False):
        return ""


class _Engine:
    def __init__(self, gguf_path=_GGUF_DEFAULT, n_ctx=_N_CTX_DEFAULT,
                 n_gpu_layers=_N_GPU_LAYERS, n_threads=None, verbose=False):
        self.lib = _load_lib()
        self.lib.llama_backend_init()
        if verbose:
            self._logcb = None
        else:
            self._logcb = _LOGCALLBACK(lambda level, text, data: None)
            self.lib.llama_log_set(self._logcb, ctypes.c_void_p(0))

        folder = os.path.dirname(os.path.abspath(gguf_path))
        self.cfg = json.load(open(os.path.join(folder, "decider_config.json")))
        (self.T, self.T_by_type), _ = _TT.from_config(self.cfg)

        mp = self.lib.llama_model_default_params()
        mp.n_gpu_layers = n_gpu_layers
        self.model = self.lib.llama_model_load_from_file(os.fsencode(gguf_path), mp)
        if not self.model:
            raise RuntimeError(f"libllama: chargement du modèle impossible : {gguf_path}")
        self.vocab = self.lib.llama_model_get_vocab(self.model)
        self.n_vocab = self.lib.llama_vocab_n_tokens(self.vocab)
        self.tok = _Tokenizer(self.lib, self.vocab)

        cp = self.lib.llama_context_default_params()
        cp.n_ctx = n_ctx
        cp.n_batch = n_ctx
        cp.n_ubatch = min(2048, n_ctx)
        cp.n_seq_max = 1
        if n_threads:
            cp.n_threads = cp.n_threads_batch = n_threads
        self.ctx = self.lib.llama_init_from_model(self.model, cp)
        if not self.ctx:
            raise RuntimeError("libllama: llama_init_from_model a échoué")
        self.n_ctx = n_ctx
        self.mem = self.lib.llama_get_memory(self.ctx)
        self.batch = self.lib.llama_batch_init(n_ctx, 0, 1)
        self.letters = np.asarray(self._letter_ids())

    def _piece_id(self):
        """pièce exacte -> id, pour les pièces majuscules nues (A..Z, AA..)."""
        if getattr(self, "_piece_ids_cache", None) is not None:
            return self._piece_ids_cache
        cache = {}
        lib = self.lib
        for tid in range(self.n_vocab):
            piece = lib.llama_vocab_get_text(self.vocab, tid)
            if not piece:
                continue
            try:
                s = piece.decode("utf-8", "ignore")
            except Exception:
                continue
            if len(s) == 1 and "A" <= s <= "Z":
                cache.setdefault(s, tid)
            elif len(s) == 2 and s.isupper() and s.isalpha():
                cache.setdefault(s, tid)
        self._piece_ids_cache = cache
        return cache

    def _letter_ids(self):
        """Ids vocabulaire des libellés A..Z, AA, AB, ... (ordre de rendu des
        options), directement depuis les pièces du vocab : exactement les ids
        qu'HuggingFace choisit (un label = une pièce). Bypass de label_table du
        package (llama.cpp insère un espace devant les tokens début-de-mot, ce
        qui casse tok.encode(n) == [id])."""
        _ALPHA = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        names = list(_ALPHA) + [a + b for a in _ALPHA for b in _ALPHA]
        piece_ids = self._piece_id()
        ids, seen = [], set()
        for n in names:
            tid = piece_ids.get(n)
            if tid is not None and tid not in seen:
                ids.append(tid)
                seen.add(tid)
            if len(ids) == 255:
                break
        if len(ids) < 10:
            raise RuntimeError(
                f"vocab trop pauvre en libellés majuscules nus ({len(ids)} trouvés < 10)"
            )
        return ids

    def _decode_single(self, item):
        """Décodage d'un article : un llama_decode (séq. 0 unique).

        NOTE : le batch multi-séquences est impossible sur ce build HIP de
        libllama (llama_decode n'accepte que seq_id == 0, même pour une seule
        séquence id=5 -> rc=-1). On reste donc mono-séquence ; le gain de
        perf vient du cache persistant des verdicts (voir DeciderScoreNode)."""
        lib = self.lib
        b = self.batch
        ids, slots = item["ids"], item["slots"]
        n = len(ids)
        if n > self.n_ctx:
            raise ValueError(f"prompt de {n} tokens dépasse n_ctx {self.n_ctx}")
        want = set(slots)
        for i, t in enumerate(ids):
            b.token[i] = t
            b.pos[i] = i
            b.n_seq_id[i] = 1
            b.seq_id[i][0] = 0
            b.logits[i] = int(i in want)
        b.n_tokens = n
        lib.llama_memory_clear(self.mem, self.ctx)
        rc = lib.llama_decode(self.ctx, b)
        if rc != 0:
            raise RuntimeError(f"llama_decode a échoué (rc={rc})")
        rows = []
        for s in slots:
            p = lib.llama_get_logits_ith(self.ctx, s)
            if not p:
                raise RuntimeError(f"llama_get_logits_ith({s}) a renvoyé NULL")
            rows.append(np.ctypeslib.as_array(p, shape=(self.n_vocab,))[self.letters].astype(np.float64))
        return rows

    def _prepare(self, sequences):
        """Construit item + températures + questions par séquence.
        sequence = (context, questions) ou (context, questions, max_ctx_tokens)."""
        prepared = []
        for seq in sequences:
            if len(seq) == 2:
                context, questions = seq
                max_ctx_tokens = _MAX_CTX_TOKENS
            else:
                context, questions, max_ctx_tokens = seq
            for q in questions:
                if not (2 <= len(q["options"]) <= _prompt.MAX_OPTIONS):
                    raise ValueError(f"2..{_prompt.MAX_OPTIONS} options requises")
            item = _prompt.build(
                _Example(context, [_Q(q["question"], list(q["options"]), 0) for q in questions]),
                self.tok, _NoShuffle(), max_options=_prompt.MAX_OPTIONS, max_ctx_tokens=max_ctx_tokens,
            )
            temps = _TT.for_types(self.T, self.T_by_type, ["choice"] * len(questions))
            prepared.append((item, temps, questions))
        return prepared

    def decide(self, context, questions, max_ctx_tokens=_MAX_CTX_TOKENS):
        item, temps, qss = self._prepare([(context, questions, max_ctx_tokens)])[0]
        rows = self._decode_single(item)
        return self._finalize([(temps, qss, rows)])[0]

    def _finalize(self, results):
        out = []
        for temps, qss, rows in results:
            nopts = [len(q["options"]) for q in qss]
            item_out = []
            for q, lg, n, t in zip(qss, rows, nopts, temps):
                z = lg[:n] / t
                p = np.exp(z - z.max())
                p /= p.sum()
                j = int(p.argmax())
                item_out.append(dict(
                    choice=q["options"][j],
                    confidence=float(p[j]),
                    probs=dict(zip(q["options"], p.tolist())),
                ))
            out.append(item_out)
        return out

    def free(self):
        lib = self.lib
        try:
            lib.llama_batch_free(self.batch)
        except Exception as e:
            log.warning("decider: llama_batch_free: %s", e)
        try:
            lib.llama_free(self.ctx)
        except Exception as e:
            log.warning("decider: llama_free: %s", e)
        try:
            lib.llama_free_model(self.model)
        except Exception as e:
            log.warning("decider: llama_free_model: %s", e)
        try:
            lib.llama_backend_free()
        except Exception as e:
            log.warning("decider: llama_backend_free: %s", e)
        self.batch = None
        self.ctx = None
        self.mem = None
        self.model = None
        self.vocab = None
        self.tok = None
        gc.collect()


class _Q:
    def __init__(self, text, options, gold=0):
        self.text = text
        self.options = options
        self.gold = gold


class _Example:
    def __init__(self, context, qs, task="infer", image=None):
        self.context = context
        self.qs = qs
        self.task = task
        self.image = image


class _NoShuffle:
    """Garde l'ordre des options tel que fourni (aucun shuffle)."""

    def sample(self, population, k):
        return population[:k]

    def shuffle(self, seq):
        return seq


_ENGINE = None
_ENGINE_ARGS = None


def engine_load(gguf_path=_GGUF_DEFAULT, n_ctx=_N_CTX_DEFAULT,
                n_gpu_layers=_N_GPU_LAYERS, n_threads=None, verbose=False):
    global _ENGINE, _ENGINE_ARGS
    if _ENGINE is not None:
        return _ENGINE
    _ENGINE = _Engine(gguf_path, n_ctx=n_ctx, n_gpu_layers=n_gpu_layers,
                      n_threads=n_threads, verbose=verbose)
    _ENGINE_ARGS = dict(gguf_path=gguf_path, n_ctx=n_ctx, n_gpu_layers=n_gpu_layers,
                        n_threads=n_threads, verbose=verbose)
    log.info("decider: moteur chargé (n_ctx=%s, vocab=%s)", n_ctx, _ENGINE.n_vocab)
    return _ENGINE


def engine_loaded():
    return _ENGINE is not None


def engine_free():
    global _ENGINE, _ENGINE_ARGS
    if _ENGINE is None:
        return
    eng = _ENGINE
    _ENGINE = None
    _ENGINE_ARGS = None
    eng.free()
    log.info("decider: moteur déchargé (VRAM libérée)")


def decide(context, questions, max_ctx_tokens=_MAX_CTX_TOKENS):
    eng = engine_load()
    try:
        return eng.decide(context, questions, max_ctx_tokens=max_ctx_tokens)
    except Exception:
        engine_free()
        raise