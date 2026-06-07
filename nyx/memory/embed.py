import os
import time
import voyageai

_client: voyageai.Client | None = None

def _get_client() -> voyageai.Client:
    global _client
    if _client is None:
        _client = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])
    return _client

def _call_with_retry(texts: list[str], input_type: str) -> list[list[float]]:
    client = _get_client()
    for attempt in range(3):
        try:
            result = client.embed(texts, model="voyage-3", input_type=input_type)
            return result.embeddings
        except Exception as e:
            if "429" in str(e) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError("Embedding failed after retries")

def embed(text: str) -> list[float]:
    return _call_with_retry([text], "document")[0]

def embed_query(text: str) -> list[float]:
    return _call_with_retry([text], "query")[0]

def embed_batch(texts: list[str]) -> list[list[float]]:
    return _call_with_retry(texts, "document")
