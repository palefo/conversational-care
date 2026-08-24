"""Retrieval-augmented generation for prompt-based agents.

A *RAG-based agent* is a prompt agent with ``rag_enabled`` set: same stored
system prompt, plus a ``search_documents`` tool over the files uploaded against
it. Turning the toggle off leaves the documents and their vectors untouched —
the agent simply stops being given the tool.

Three modules, one per stage:

* ``extract`` — uploaded file → plain text (.txt/.md, .pdf, .docx).
* ``ingest``  — text → overlapping chunks → embeddings, on the background pool,
  with all progress kept on the ``RagDocument`` row so a page reload (or a
  process restart) never loses a job.
* ``retrieve`` — question → the best-matching chunks, scored in numpy against
  the vectors stored on the chunk rows.

There is no vector database and no extra service: vectors live in Postgres as
packed float32 blobs. See agents.md for the rationale and the operational
notes.
"""
