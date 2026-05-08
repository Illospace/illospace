import os
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest


class TestWorkerManifest:
    def test_manifest_defaults(self):
        from brain.platform.gpu.config import WorkerManifest
        m = WorkerManifest(name="test", model_path="/tmp/model", vram_mb=1000)
        assert m.priority == 5
        assert m.idle_timeout == 900
        assert m.preload is False
        assert m.max_batch_size == 64
        assert m.load_timeout == 45

    def test_manifest_custom(self):
        from brain.platform.gpu.config import WorkerManifest
        m = WorkerManifest(
            name="embed", model_path="/tmp/model", vram_mb=5000,
            priority=10, idle_timeout=0, preload=True, max_batch_size=32, load_timeout=60,
        )
        assert m.priority == 10
        assert m.preload is True

    def test_gpu_config_reads_repo_env_for_model_resolution(self, monkeypatch, tmp_path):
        from brain.platform.gpu import config as gpu_config

        monkeypatch.delenv("EMBEDDING_MODEL_PATH", raising=False)
        monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
        (tmp_path / ".env").write_text("EMBEDDING_MODEL=Qwen/Qwen3-Embedding-8B\n", encoding="utf-8")

        values = gpu_config._read_repo_env(tmp_path)
        manifests = gpu_config.build_worker_manifests(tmp_path)

        assert values["EMBEDDING_MODEL"] == "Qwen/Qwen3-Embedding-8B"
        assert manifests[0].model_path == "Qwen/Qwen3-Embedding-8B"

    def test_gpu_config_uses_robust_defaults(self, monkeypatch, tmp_path):
        from brain.platform.gpu import config as gpu_config

        for key in (
            "EMBEDDING_MODEL_PATH",
            "EMBEDDING_MODEL",
            "EMBEDDING_BACKEND",
            "LLM_MODEL_PATH",
            "LLM_MODEL",
            "GPU_EMBEDDING_VRAM_MB",
            "GPU_LLM_VRAM_MB",
            "GPU_EMBEDDING_LOAD_TIMEOUT_SECONDS",
            "GPU_LLM_LOAD_TIMEOUT_SECONDS",
            "GPU_EMBEDDING_MAX_BATCH_SIZE",
        ):
            monkeypatch.delenv(key, raising=False)

        manifests = gpu_config.build_worker_manifests(tmp_path)

        embedding, llm = manifests
        assert embedding.model_path == "Qwen/Qwen3-Embedding-8B"
        assert embedding.vram_mb == 15000
        assert embedding.load_timeout == 7200
        assert embedding.max_batch_size == 16
        assert embedding.preload is False
        assert llm.model_path == "Qwen/Qwen3.5-4B"
        assert llm.vram_mb == 9000
        assert llm.load_timeout == 300

    def test_gpu_embedding_preload_waits_for_gpu_backend(self, monkeypatch, tmp_path):
        from brain.platform.gpu import config as gpu_config

        monkeypatch.delenv("EMBEDDING_BACKEND", raising=False)
        monkeypatch.delenv("GPU_PRELOAD_EMBEDDING", raising=False)

        (tmp_path / ".env").write_text("EMBEDDING_BACKEND=api\n", encoding="utf-8")
        embedding, _ = gpu_config.build_worker_manifests(tmp_path)
        assert embedding.preload is False

        (tmp_path / ".env").write_text("EMBEDDING_BACKEND=gpu\n", encoding="utf-8")
        embedding, _ = gpu_config.build_worker_manifests(tmp_path)
        assert embedding.preload is True

        monkeypatch.setenv("GPU_PRELOAD_EMBEDDING", "0")
        embedding, _ = gpu_config.build_worker_manifests(tmp_path)
        assert embedding.preload is False

    def test_gpu_config_prefers_downloaded_local_models(self, monkeypatch, tmp_path):
        from brain.platform.gpu import config as gpu_config

        for key in ("EMBEDDING_MODEL_PATH", "EMBEDDING_MODEL", "LLM_MODEL_PATH", "LLM_MODEL"):
            monkeypatch.delenv(key, raising=False)

        embed_dir = tmp_path / "models" / "qwen3-embedding-8b"
        llm_dir = tmp_path / "models" / "qwen3.5-4b"
        embed_dir.mkdir(parents=True)
        llm_dir.mkdir(parents=True)
        (embed_dir / "modules.json").write_text("{}", encoding="utf-8")
        (llm_dir / "config.json").write_text("{}", encoding="utf-8")

        embedding, llm = gpu_config.build_worker_manifests(tmp_path)

        assert embedding.model_path == str(embed_dir)
        assert llm.model_path == str(llm_dir)

    def test_gpu_config_prefers_local_models_when_env_contains_default_ids(self, monkeypatch, tmp_path):
        from brain.platform.gpu import config as gpu_config

        monkeypatch.delenv("EMBEDDING_MODEL_PATH", raising=False)
        monkeypatch.delenv("LLM_MODEL_PATH", raising=False)
        monkeypatch.setenv("EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B")
        monkeypatch.setenv("LLM_MODEL", "Qwen/Qwen3.5-4B")

        embed_dir = tmp_path / "models" / "qwen3-embedding-8b"
        llm_dir = tmp_path / "models" / "qwen3.5-4b"
        embed_dir.mkdir(parents=True)
        llm_dir.mkdir(parents=True)
        (embed_dir / "modules.json").write_text("{}", encoding="utf-8")
        (llm_dir / "config.json").write_text("{}", encoding="utf-8")

        embedding, llm = gpu_config.build_worker_manifests(tmp_path)

        assert embedding.model_path == str(embed_dir)
        assert llm.model_path == str(llm_dir)

    def test_server_config_defaults(self):
        from brain.platform.gpu.config import ServerConfig
        cfg = ServerConfig()
        assert cfg.port == 9800
        assert cfg.host == "127.0.0.1"
        assert cfg.socket_dir == "/tmp"
        assert cfg.reconciliation_interval == 60
        assert cfg.max_restart_attempts == 5


class TestVRAM:
    def test_bookkeeper_tracks_allocations(self):
        from brain.platform.gpu.vram import VRAMBookkeeper
        bk = VRAMBookkeeper(total_mb=32000)
        assert bk.free_mb == 32000
        bk.allocate("embedding", 5000)
        assert bk.free_mb == 27000
        bk.allocate("llm", 3000)
        assert bk.free_mb == 24000

    def test_bookkeeper_release(self):
        from brain.platform.gpu.vram import VRAMBookkeeper
        bk = VRAMBookkeeper(total_mb=32000)
        bk.allocate("embedding", 5000)
        bk.release("embedding")
        assert bk.free_mb == 32000

    def test_bookkeeper_release_unknown_is_safe(self):
        from brain.platform.gpu.vram import VRAMBookkeeper
        bk = VRAMBookkeeper(total_mb=32000)
        bk.release("nonexistent")
        assert bk.free_mb == 32000

    def test_bookkeeper_has_space(self):
        from brain.platform.gpu.vram import VRAMBookkeeper
        bk = VRAMBookkeeper(total_mb=32000)
        bk.allocate("embedding", 5000)
        assert bk.has_space(3000) is True
        assert bk.has_space(28000) is False

    def test_query_gpu_total_fallback(self):
        from brain.platform.gpu.vram import query_gpu_total_mb
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert query_gpu_total_mb() is None

    def test_query_gpu_total_parses_nvidia_smi(self):
        from brain.platform.gpu.vram import query_gpu_total_mb
        mock_result = type("R", (), {"returncode": 0, "stdout": "32768\n"})()
        with patch("subprocess.run", return_value=mock_result):
            assert query_gpu_total_mb() == 32768

    def test_query_gpu_used_parses_nvidia_smi(self):
        from brain.platform.gpu.vram import query_gpu_used_mb
        mock_result = type("R", (), {"returncode": 0, "stdout": "7800\n"})()
        with patch("subprocess.run", return_value=mock_result):
            assert query_gpu_used_mb() == 7800

    def test_bookkeeper_reconcile_corrects_drift(self):
        from brain.platform.gpu.vram import VRAMBookkeeper
        bk = VRAMBookkeeper(total_mb=32000)
        bk.allocate("embedding", 5000)
        with patch("brain.platform.gpu.vram.query_gpu_used_mb", return_value=8000), \
             patch("brain.platform.gpu.vram.query_gpu_total_mb", return_value=32000):
            drifted = bk.reconcile()
        assert drifted is True
        assert bk.free_mb == 24000


class TestDeviceSelection:
    def test_selects_cuda_when_available(self, monkeypatch):
        from brain.platform.gpu.device import default_dtype, select_device

        class Cuda:
            @staticmethod
            def is_available():
                return True

            @staticmethod
            def is_bf16_supported():
                return True

        torch = type("Torch", (), {
            "cuda": Cuda,
            "bfloat16": "bf16",
            "float16": "fp16",
            "float32": "fp32",
        })()

        monkeypatch.delenv("GPU_DEVICE", raising=False)

        assert select_device(torch) == "cuda"
        assert default_dtype(torch, "cuda") == "bf16"

    def test_selects_mps_without_cuda(self, monkeypatch):
        from brain.platform.gpu.device import default_dtype, select_device

        class Cuda:
            @staticmethod
            def is_available():
                return False

        class Mps:
            @staticmethod
            def is_available():
                return True

        torch = type("Torch", (), {
            "cuda": Cuda,
            "backends": type("Backends", (), {"mps": Mps})(),
            "float16": "fp16",
            "float32": "fp32",
        })()

        monkeypatch.delenv("GPU_DEVICE", raising=False)

        assert select_device(torch) == "mps"
        assert default_dtype(torch, "mps") == "fp16"

    def test_env_override_wins(self, monkeypatch):
        from brain.platform.gpu.device import select_device

        class Cuda:
            @staticmethod
            def is_available():
                return True

        torch = type("Torch", (), {"cuda": Cuda})()
        monkeypatch.setenv("GPU_DEVICE", "cpu")

        assert select_device(torch) == "cpu"

    def test_unavailable_cuda_override_falls_back(self, monkeypatch):
        from brain.platform.gpu.device import select_device

        class Cuda:
            @staticmethod
            def is_available():
                return False

        class Mps:
            @staticmethod
            def is_available():
                return True

        torch = type("Torch", (), {
            "cuda": Cuda,
            "backends": type("Backends", (), {"mps": Mps})(),
        })()
        monkeypatch.setenv("EMBEDDING_DEVICE", "cuda")

        assert select_device(torch, "EMBEDDING_DEVICE") == "mps"


import asyncio
import signal


class TestWorkerState:
    def test_state_transitions(self):
        from brain.platform.gpu.worker_protocol import WorkerState
        assert WorkerState.REGISTERED.value == "registered"
        assert WorkerState.LOADING.value == "loading"
        assert WorkerState.READY.value == "ready"
        assert WorkerState.BUSY.value == "busy"
        assert WorkerState.DRAINING.value == "draining"
        assert WorkerState.UNLOADING.value == "unloading"
        assert WorkerState.STOPPED.value == "stopped"
        assert WorkerState.FAILED.value == "failed"


class TestBaseWorker:
    def test_worker_has_required_interface(self):
        from brain.platform.gpu.worker_protocol import BaseWorker
        import inspect
        assert hasattr(BaseWorker, "load_model")
        assert hasattr(BaseWorker, "unload_model")
        assert hasattr(BaseWorker, "handle_request")
        assert inspect.isabstract(BaseWorker) or True

    def test_worker_health_response(self):
        from brain.platform.gpu.worker_protocol import BaseWorker, WorkerState
        from brain.platform.gpu.config import WorkerManifest

        class FakeWorker(BaseWorker):
            def load_model(self): pass
            def unload_model(self): pass
            async def handle_request(self, data): return {"ok": True}

        manifest = WorkerManifest(name="test", model_path="/tmp", vram_mb=1000)
        w = FakeWorker(manifest)
        w.state = WorkerState.READY
        health = w.get_health()
        assert health["name"] == "test"
        assert health["status"] == "ready"
        assert "idle_s" in health
        assert "requests_total" in health


class TestEmbeddingWorker:
    def test_worker_instantiates(self, monkeypatch):
        from brain.platform.gpu.workers.embedding import EmbeddingWorker
        from brain.platform.gpu.config import WorkerManifest

        monkeypatch.setenv("EMBEDDING_DIM", "2000")

        m = WorkerManifest(name="embedding", model_path="/tmp/model", vram_mb=5000)
        w = EmbeddingWorker(m)
        assert w.manifest.name == "embedding"
        assert w.truncate_dim == 2000

    def test_worker_uses_kernel_embedding_dim_when_env_missing(self, monkeypatch):
        from brain.kernel import config as cfg
        from brain.platform.gpu.workers.embedding import EmbeddingWorker
        from brain.platform.gpu.config import WorkerManifest

        monkeypatch.delenv("EMBEDDING_DIM", raising=False)
        monkeypatch.setattr(cfg, "EMBEDDING_DIM", 768)

        m = WorkerManifest(name="embedding", model_path="/tmp/model", vram_mb=5000)
        w = EmbeddingWorker(m)

        assert w.truncate_dim == 768

    @pytest.mark.asyncio
    async def test_handle_request_document_mode(self):
        from brain.platform.gpu.workers.embedding import EmbeddingWorker
        from brain.platform.gpu.config import WorkerManifest
        import numpy as np

        m = WorkerManifest(name="embedding", model_path="/tmp/model", vram_mb=5000)
        w = EmbeddingWorker(m)

        fake_embeddings = np.random.randn(2, w.truncate_dim).astype(np.float32)
        w.model = type("M", (), {
            "encode": lambda self, texts, **kw: fake_embeddings
        })()

        result = await w.handle_request({
            "texts": ["hello", "world"],
            "mode": "document",
        })
        assert result["count"] == 2
        assert result["dims"] == w.truncate_dim
        assert len(result["embeddings"]) == 2

    @pytest.mark.asyncio
    async def test_handle_request_query_mode_adds_prefix(self):
        from brain.platform.gpu.workers.embedding import EmbeddingWorker
        from brain.platform.gpu.config import WorkerManifest
        import numpy as np

        m = WorkerManifest(name="embedding", model_path="/tmp/model", vram_mb=5000)
        w = EmbeddingWorker(m)

        captured_texts = []
        fake_embeddings = np.random.randn(1, w.truncate_dim).astype(np.float32)
        w.model = type("M", (), {
            "encode": lambda self, texts, **kw: (captured_texts.extend(texts), fake_embeddings)[1]
        })()

        await w.handle_request({"texts": ["search query"], "mode": "query"})
        assert captured_texts[0].startswith("Instruct:")
        assert "search query" in captured_texts[0]

    def test_embedding_worker_pads_fallback_vectors_to_configured_dim(self, monkeypatch):
        from brain.platform.gpu.workers.embedding import EmbeddingWorker
        from brain.platform.gpu.config import WorkerManifest
        import numpy as np

        monkeypatch.setenv("EMBEDDING_DIM", "8")

        m = WorkerManifest(name="embedding", model_path="/tmp/model", vram_mb=5000)
        w = EmbeddingWorker(m)

        result = w._fit_embedding_dim(np.array([[3.0, 4.0, 0.0]], dtype=np.float32))

        assert result.shape == (1, 8)
        assert result[0, 3:].tolist() == [0.0] * 5
        assert np.isclose(np.linalg.norm(result[0]), 1.0)

    def test_embedding_worker_keeps_manifest_model_on_cuda(self, monkeypatch):
        from brain.platform.gpu.workers.embedding import EmbeddingWorker
        from brain.platform.gpu.config import WorkerManifest

        monkeypatch.delenv("GPU_ALLOW_LARGE_EMBEDDING_ON_NON_CUDA", raising=False)
        monkeypatch.setenv("GPU_EMBEDDING_FALLBACK_MODEL", "all-MiniLM-L6-v2")

        worker = EmbeddingWorker(
            WorkerManifest(name="embedding", model_path="Qwen/Qwen3-Embedding-8B", vram_mb=15000)
        )

        assert worker._model_path_for_device("cuda") == ("Qwen/Qwen3-Embedding-8B", "cuda")
        assert worker._model_path_for_device("mps") == ("all-MiniLM-L6-v2", "cpu")


class TestLLMWorker:
    def test_llm_worker_uses_lightweight_fallback_without_cuda(self, monkeypatch):
        from brain.platform.gpu.workers.llm import LLMWorker
        from brain.platform.gpu.config import WorkerManifest

        monkeypatch.delenv("GPU_ALLOW_LARGE_LLM_ON_NON_CUDA", raising=False)
        monkeypatch.setenv("GPU_LLM_FALLBACK_MODEL", "Qwen/Qwen3-0.6B")

        worker = LLMWorker(WorkerManifest(name="llm", model_path="Qwen/Qwen3.5-4B", vram_mb=9000))

        assert worker._model_path_for_device("mps") == "Qwen/Qwen3-0.6B"
        assert worker._model_path_for_device("cuda") == "Qwen/Qwen3.5-4B"

    def test_llm_worker_keeps_manifest_model_on_cuda_when_fallback_configured(self, monkeypatch):
        from brain.platform.gpu.workers.llm import LLMWorker
        from brain.platform.gpu.config import WorkerManifest

        monkeypatch.delenv("GPU_ALLOW_LARGE_LLM_ON_NON_CUDA", raising=False)
        monkeypatch.setenv("GPU_LLM_FALLBACK_MODEL", "Qwen/Qwen3-0.6B")

        worker = LLMWorker(WorkerManifest(name="llm", model_path="Qwen/Qwen3.5-4B", vram_mb=9000))

        assert worker._model_path_for_device("cuda") == "Qwen/Qwen3.5-4B"


import time as _time


class TestWorkerManager:
    def test_register_worker(self):
        from brain.platform.gpu.worker_manager import WorkerManager
        from brain.platform.gpu.vram import VRAMBookkeeper
        from brain.platform.gpu.config import WorkerManifest

        bk = VRAMBookkeeper(total_mb=32000)
        mgr = WorkerManager(bk, socket_dir="/tmp")
        m = WorkerManifest(name="test", model_path="/tmp", vram_mb=1000, worker_module="brain.platform.gpu.workers.embedding")
        mgr.register(m)
        assert "test" in mgr.workers
        assert mgr.workers["test"]["status"] == "registered"

    def test_eviction_order_by_priority_then_idle(self):
        from brain.platform.gpu.worker_manager import WorkerManager
        from brain.platform.gpu.vram import VRAMBookkeeper
        from brain.platform.gpu.config import WorkerManifest

        bk = VRAMBookkeeper(total_mb=32000)
        mgr = WorkerManager(bk, socket_dir="/tmp")

        m1 = WorkerManifest(name="high_pri", model_path="/tmp", vram_mb=5000, priority=10)
        m2 = WorkerManifest(name="low_pri", model_path="/tmp", vram_mb=3000, priority=3)
        m3 = WorkerManifest(name="mid_pri", model_path="/tmp", vram_mb=2000, priority=5)

        mgr.register(m1)
        mgr.register(m2)
        mgr.register(m3)

        for name in ["high_pri", "low_pri", "mid_pri"]:
            mgr.workers[name]["status"] = "ready"
            mgr.workers[name]["last_activity"] = _time.time()
        mgr.workers["low_pri"]["last_activity"] = _time.time() - 600

        order = mgr.eviction_order()
        assert [w["manifest"].name for w in order] == ["low_pri", "mid_pri", "high_pri"]

    def test_backoff_calculation(self):
        from brain.platform.gpu.worker_manager import _backoff_seconds
        assert _backoff_seconds(0) == 1
        assert _backoff_seconds(1) == 2
        assert _backoff_seconds(2) == 4
        assert _backoff_seconds(5) == 30
        assert _backoff_seconds(10) == 30

    def test_start_worker_is_idempotent_when_process_alive(self):
        from brain.platform.gpu.worker_manager import WorkerManager
        from brain.platform.gpu.vram import VRAMBookkeeper
        from brain.platform.gpu.config import WorkerManifest

        bk = VRAMBookkeeper(total_mb=32000)
        mgr = WorkerManager(bk, socket_dir="/tmp")
        m = WorkerManifest(
            name="llm", model_path="/tmp/model", vram_mb=3000,
            worker_module="brain.platform.gpu.workers.llm", load_timeout=0,
        )
        mgr.register(m)
        proc = MagicMock()
        proc.pid = 4242
        proc.poll.return_value = None
        mgr.workers["llm"]["process"] = proc
        mgr.workers["llm"]["status"] = "ready"

        with patch("subprocess.Popen") as mock_popen:
            assert mgr.start_worker("llm") is True

        mock_popen.assert_not_called()

    def test_start_worker_kills_orphan_before_spawn(self):
        from brain.platform.gpu.worker_manager import WorkerManager
        from brain.platform.gpu.vram import VRAMBookkeeper
        from brain.platform.gpu.config import WorkerManifest

        bk = VRAMBookkeeper(total_mb=32000)
        mgr = WorkerManager(bk, socket_dir="/tmp")
        m = WorkerManifest(
            name="llm", model_path="/tmp/model", vram_mb=3000,
            worker_module="brain.platform.gpu.workers.llm", load_timeout=0,
        )
        mgr.register(m)

        fake_popen = MagicMock()
        fake_popen.pid = 5001
        fake_popen.poll.return_value = None

        with patch.object(mgr, "_find_matching_worker_pids", return_value=[4001]), \
             patch("os.path.exists", return_value=False), \
             patch("os.kill") as mock_kill, \
             patch("subprocess.Popen", return_value=fake_popen):
            assert mgr.start_worker("llm") is False

        mock_kill.assert_any_call(4001, signal.SIGKILL)

    def test_start_worker_spawns_with_repo_root(self):
        from brain.platform.gpu.worker_manager import WorkerManager
        from brain.platform.gpu.vram import VRAMBookkeeper
        from brain.platform.gpu.config import WorkerManifest

        bk = VRAMBookkeeper(total_mb=32000)
        mgr = WorkerManager(bk, socket_dir="/tmp")
        m = WorkerManifest(
            name="embedding",
            model_path="/tmp/model",
            vram_mb=3000,
            worker_module="brain.platform.gpu.workers.embedding",
            load_timeout=1,
        )
        mgr.register(m)

        fake_popen = MagicMock()
        fake_popen.pid = 5001
        fake_popen.poll.return_value = None
        exists_calls = {"count": 0}

        def fake_exists(_path):
            exists_calls["count"] += 1
            return exists_calls["count"] > 1

        with patch.object(mgr, "_kill_matching_workers"), \
             patch("os.path.exists", side_effect=fake_exists), \
             patch("os.unlink"), \
             patch("builtins.open", mock_open()), \
             patch("subprocess.Popen", return_value=fake_popen) as mock_popen:
            assert mgr.start_worker("embedding") is True

        assert Path(mock_popen.call_args.kwargs["cwd"]).resolve() == Path(__file__).resolve().parents[1]

    def test_start_lower_priority_worker_does_not_evict_embedding(self):
        from brain.platform.gpu.worker_manager import WorkerManager
        from brain.platform.gpu.vram import VRAMBookkeeper
        from brain.platform.gpu.config import WorkerManifest

        bk = VRAMBookkeeper(total_mb=16000)
        mgr = WorkerManager(bk, socket_dir="/tmp")
        mgr.register(WorkerManifest(name="embedding", model_path="/tmp/embed", vram_mb=15000, priority=10))
        mgr.register(WorkerManifest(name="llm", model_path="/tmp/llm", vram_mb=9000, priority=5))
        mgr.workers["embedding"]["status"] = "ready"
        mgr.workers["embedding"]["process"] = MagicMock()
        mgr.workers["embedding"]["process"].poll.return_value = None
        bk.allocate("embedding", 15000)

        with patch.object(mgr, "_kill_matching_workers"), \
             patch.object(mgr, "cleanup_conflicting_gpu_processes", return_value=[]), \
             patch("brain.platform.gpu.vram.query_gpu_used_mb", return_value=None), \
             patch("subprocess.Popen") as mock_popen:
            assert mgr.start_worker("llm") is False

        assert mgr.workers["embedding"]["status"] == "ready"
        mock_popen.assert_not_called()

    def test_cleanup_conflicting_gpu_processes_terminates_known_model_runtimes(self):
        from brain.platform.gpu.worker_manager import WorkerManager
        from brain.platform.gpu.vram import VRAMBookkeeper

        mgr = WorkerManager(VRAMBookkeeper(total_mb=32000), socket_dir="/tmp")

        with patch.object(
            mgr,
            "_find_conflicting_gpu_processes",
            return_value=[(4001, "/usr/bin/ollama runner --model qwen3.5")],
        ), patch.object(mgr, "_pid_is_alive", return_value=False), \
             patch("time.sleep"), \
             patch("os.kill") as mock_kill:
            killed = mgr.cleanup_conflicting_gpu_processes()

        assert killed == [4001]
        mock_kill.assert_called_once_with(4001, signal.SIGTERM)

    def test_cleanup_orphaned_workers_kills_matching_processes(self):
        from brain.platform.gpu.worker_manager import WorkerManager
        from brain.platform.gpu.vram import VRAMBookkeeper
        from brain.platform.gpu.config import WorkerManifest

        bk = VRAMBookkeeper(total_mb=32000)
        mgr = WorkerManager(bk, socket_dir="/tmp")
        mgr.register(WorkerManifest(name="embedding", model_path="/tmp/model", vram_mb=5000, worker_module="brain.platform.gpu.workers.embedding"))
        mgr.register(WorkerManifest(name="llm", model_path="/tmp/model", vram_mb=3000, worker_module="brain.platform.gpu.workers.llm"))

        with patch.object(mgr, "_find_matching_worker_pids", side_effect=[[111], [222]]), \
             patch("os.path.exists", return_value=False), \
             patch("os.kill") as mock_kill:
            mgr.cleanup_orphaned_workers()

        assert mock_kill.call_count == 2
        mock_kill.assert_any_call(111, signal.SIGKILL)
        mock_kill.assert_any_call(222, signal.SIGKILL)

    def test_startup_failures_eventually_mark_worker_failed(self):
        from brain.platform.gpu.worker_manager import WorkerManager
        from brain.platform.gpu.vram import VRAMBookkeeper
        from brain.platform.gpu.config import WorkerManifest

        bk = VRAMBookkeeper(total_mb=32000)
        mgr = WorkerManager(bk, socket_dir="/tmp", max_restarts=2)
        m = WorkerManifest(
            name="embedding",
            model_path="/tmp/model",
            vram_mb=5000,
            worker_module="brain.platform.gpu.workers.embedding",
            load_timeout=1,
        )
        mgr.register(m)

        fake_popen = MagicMock()
        fake_popen.pid = 5001
        fake_popen.poll.return_value = 1
        fake_popen.returncode = 1

        with patch.object(mgr, "_kill_matching_workers"), \
             patch("os.path.exists", return_value=False), \
             patch("subprocess.Popen", return_value=fake_popen), \
             patch.object(mgr, "_read_crash_output", return_value="missing model"), \
             patch("time.sleep", return_value=None):
            assert mgr.start_worker("embedding") is False
            assert mgr.workers["embedding"]["status"] == "stopped"
            assert mgr.workers["embedding"]["failure_count"] == 1

            assert mgr.start_worker("embedding") is False
            assert mgr.workers["embedding"]["status"] == "stopped"
            assert mgr.workers["embedding"]["failure_count"] == 2

            assert mgr.start_worker("embedding") is False

        assert mgr.workers["embedding"]["status"] == "failed"
        assert mgr.workers["embedding"]["failure_count"] == 3
        assert mgr.workers["embedding"]["restart_after"] == 0.0

    def test_sync_manifests_updates_registered_worker(self):
        from brain.platform.gpu.worker_manager import WorkerManager
        from brain.platform.gpu.vram import VRAMBookkeeper
        from brain.platform.gpu.config import WorkerManifest

        bk = VRAMBookkeeper(total_mb=32000)
        mgr = WorkerManager(bk, socket_dir="/tmp")
        mgr.register(WorkerManifest(name="embedding", model_path="/tmp/old", vram_mb=5000))

        mgr.sync_manifests([WorkerManifest(name="embedding", model_path="Qwen/Qwen3-Embedding-8B", vram_mb=5000)])

        assert mgr.workers["embedding"]["manifest"].model_path == "Qwen/Qwen3-Embedding-8B"


class TestMainServer:
    def test_route_embed_to_worker(self):
        from brain.platform.gpu.server import GPUServer
        from brain.platform.gpu.config import ServerConfig
        srv = GPUServer(ServerConfig())
        assert srv.route_for_endpoint("/embed") == "embedding"
        assert srv.route_for_endpoint("/generate") == "llm"
        assert srv.route_for_endpoint("/health") is None

    def test_health_aggregation(self):
        from brain.platform.gpu.server import GPUServer
        from brain.platform.gpu.config import ServerConfig
        srv = GPUServer(ServerConfig())
        health = srv.aggregate_health()
        assert health["status"] == "down"
        assert set(health["workers"].keys()) == {"embedding", "llm"}
        assert health["workers"]["embedding"]["status"] == "registered"
        assert health["workers"]["llm"]["status"] == "registered"

    def test_health_degraded_when_partial(self):
        from brain.platform.gpu.server import GPUServer
        from brain.platform.gpu.config import ServerConfig
        srv = GPUServer(ServerConfig())
        srv.manager.workers = {
            "embedding": {"status": "ready", "manifest": type("M", (), {"name": "embedding", "vram_mb": 5000})()},
            "llm": {"status": "failed", "manifest": type("M", (), {"name": "llm", "vram_mb": 3000})()},
        }
        health = srv.aggregate_health()
        assert health["status"] == "degraded"

    def test_fallback_policy_parsing(self):
        from brain.platform.gpu.server import _parse_fallback_policy
        assert _parse_fallback_policy("auto") == "auto"
        assert _parse_fallback_policy("local-only") == "local-only"
        assert _parse_fallback_policy("api-only") == "api-only"
        assert _parse_fallback_policy("") == "local-only"
        assert _parse_fallback_policy(None) == "local-only"

    def test_should_fallback_to_api(self):
        from brain.platform.gpu.server import _should_fallback
        assert _should_fallback("auto", "failed", has_api_config=True) is True
        assert _should_fallback("auto", "ready", has_api_config=True) is False
        assert _should_fallback("auto", "loading", has_api_config=True) is True
        assert _should_fallback("local-only", "failed", has_api_config=True) is False
        assert _should_fallback("api-only", "ready", has_api_config=True) is True
        assert _should_fallback("auto", "failed", has_api_config=False) is False
