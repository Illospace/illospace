-- Old Slack snapshots have no channel privacy proof. Delete this derived index.
DELETE FROM items WHERE kind='slack';
