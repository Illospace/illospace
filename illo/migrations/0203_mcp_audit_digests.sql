-- Redacted arguments cannot produce a new digest. Clear old guessable hashes.
UPDATE audit SET args_digest = 'unavailable';
