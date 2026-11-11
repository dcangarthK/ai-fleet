Real Docker Model Runner output captured on Laptop 2 (M1 16 GB) by the owner on 2026-11-12.
Docker server 28.4.0, Model Runner v0.1.41, llama.cpp latest-metal.
Both /engines/v1/models and /engines/llama.cpp/v1/models returned models.json.
The listener is loopback-only (lsof_12434.txt).
ai/qwen3 and ai/deepcoder-preview were pulled as images, not models, so they are absent here. Fix:
`docker model pull ai/qwen3 ai/deepcoder-preview`.
