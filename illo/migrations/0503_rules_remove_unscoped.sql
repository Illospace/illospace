-- Old card and proposal rules lack the scope fixed in later versions.
UPDATE rules SET removed_at=now()
WHERE removed_at IS NULL AND origin <> 'file'
  AND (NOT (match ? 'lane_source') OR NOT (match ? 'repository'));
