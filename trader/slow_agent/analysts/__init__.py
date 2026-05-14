"""Per-domain analyst nodes. Each one queries its data source, asks the
fast LLM to write a focused report, and writes the report into the
matching `*_report` field of the graph state."""
