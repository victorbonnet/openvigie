-- Anthropic 1-hour cache writes are priced differently from 5-minute ones.

ALTER TABLE usage_records ADD COLUMN cache_creation_1h_input_tokens INTEGER;
