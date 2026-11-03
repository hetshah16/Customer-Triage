"""
Loads all markdown files from data/ corpus into ChromaDB.
Exposes retrieve(query, company) -> list of relevant text chunks.
"""
import glob
import os
import re

import chromadb


# Deterministic seed for reproducibility
COLLECTION_NAME = "support_corpus"
DATA_ROOT = os.path.join(os.path.dirname(__file__), "..", "data")

CANONICAL_COMPANIES = {
    "hackerrank": "HackerRank",
    "claude": "Claude",
    "visa": "Visa"
}
INVALID_COMPANY_VALUES = {"", "none", "null", "nan", "na", "n/a"}
IGNORE_PATH_SEGMENTS = {"__pycache__"}
MAX_CHUNKS_PER_FILE = 50


def normalize_company(company):
    """Normalize company values from CSV input or folder names."""
    if company is None:
        return None
    value = str(company).strip()
    if not value:
        return None

    normalized = value.lower().strip()
    if normalized in INVALID_COMPANY_VALUES:
        return None

    return CANONICAL_COMPANIES.get(normalized, value.title())


def _get_collection():
    """Initialize ChromaDB in-memory client with default embedding function."""
    client = chromadb.Client()
    ef = chromadb.utils.embedding_functions.DefaultEmbeddingFunction()
    return client, ef


def _is_ignored_path(relpath):
    """Skip hidden or special directories inside the data corpus."""
    for segment in relpath.split(os.sep):
        if segment.startswith(".") or segment in IGNORE_PATH_SEGMENTS:
            return True
    return False


def split_text_into_chunks(text, max_chars=500, min_chars=50):
    """Split body text into meaningful chunks suitable for embedding. Preserves paragraph and sentence boundaries."""
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if len(p.strip()) >= min_chars]
    if not paragraphs and len(text.strip()) >= min_chars:
        paragraphs = [text.strip()]

    chunks = []
    for paragraph in paragraphs:
        if len(paragraph) <= max_chars:
            chunks.append(paragraph)
            continue

        sentences = re.split(r"(?<=[.!?])\s+", paragraph)
        current = ""
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) > max_chars:
                for start in range(0, len(sentence), max_chars):
                    part = sentence[start : start + max_chars].strip()
                    if len(part) >= min_chars:
                        chunks.append(part)
                current = ""
                continue

            if not current:
                current = sentence
                continue

            if len(current) + 1 + len(sentence) <= max_chars:
                current = f"{current} {sentence}"
            else:
                chunks.append(current.strip())
                current = sentence

        if current:
            chunks.append(current.strip())

    return [chunk for chunk in chunks if len(chunk) >= min_chars]


def build_retriever():
    """Load all .md files → chunk → embed → store in ChromaDB. Returns collection."""
    print("[RETRIEVER_INIT] -> Loading corpus from data/...")
    client, ef = _get_collection()

    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass

    collection = client.create_collection(
        name=COLLECTION_NAME,
        embedding_function=ef,
    )

    docs, ids, metas = [], [], []
    total_chunks = 0
    all_md = sorted(glob.glob(os.path.join(DATA_ROOT, "**", "*.md"), recursive=True))

    for i, filepath in enumerate(all_md):
        rel = os.path.relpath(filepath, DATA_ROOT)
        if _is_ignored_path(rel):
            print(f"[RETRIEVER_INIT] -> Skipping ignored path: {rel}")
            continue

        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read().strip()
        if not content:
            continue

        folder_name = rel.split(os.sep)[0].strip().lower()
        company = normalize_company(folder_name) or folder_name.title()

        chunks = split_text_into_chunks(content, max_chars=500)
        if len(chunks) > MAX_CHUNKS_PER_FILE:
            print(f"[RETRIEVER_INIT] -> File {rel} produced {len(chunks)} chunks; limiting to {MAX_CHUNKS_PER_FILE}.")
            chunks = chunks[:MAX_CHUNKS_PER_FILE]

        for j, chunk in enumerate(chunks):
            docs.append(chunk)
            ids.append(f"{folder_name}_{i}_{j}")
            metas.append({"company": company, "source": filepath})

        BATCH_SIZE = 500
        while len(docs) >= BATCH_SIZE:
            batch_docs = docs[:BATCH_SIZE]
            batch_ids = ids[:BATCH_SIZE]
            batch_metas = metas[:BATCH_SIZE]
            collection.add(
                documents=batch_docs,
                ids=batch_ids,
                metadatas=batch_metas,
            )
            total_chunks += len(batch_docs)
            print(f"[RETRIEVER_INIT] -> Indexed batch | {total_chunks} chunks so far from {len(all_md)} files")
            docs, ids, metas = docs[BATCH_SIZE:], ids[BATCH_SIZE:], metas[BATCH_SIZE:]

    if docs:
        collection.add(documents=docs, ids=ids, metadatas=metas)
        total_chunks += len(docs)
        print(f"[RETRIEVER_INIT] -> Indexed final batch | {total_chunks} chunks total")

    print(f"[RETRIEVER_INIT] -> Indexed {total_chunks} chunks from {len(all_md)} files.")
    return collection


def retrieve(collection, query: str, company: str = None, n: int = 3):
    """Retrieve relevant document chunks from ChromaDB, with optional company filtering."""
    print(f"[RETRIEVER] -> Searching for: '{query[:50]}...' | Filter: {company}")

    normalized_company = normalize_company(company)
    search_filter = None
    if normalized_company:
        search_filter = {"company": normalized_company}

    try:
        results = collection.query(
            query_texts=[query],
            n_results=n,
            where=search_filter,
        )

        documents = results.get("documents", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]

        enriched_chunks = []
        for i in range(len(documents)):
            enriched_chunks.append(documents[i])

        return enriched_chunks
    except Exception as e:
        print(f"[RETRIEVER_ERR] -> {e}")
        return []
