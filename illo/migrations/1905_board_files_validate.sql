SET LOCAL lock_timeout = '1s';

ALTER TABLE page_files VALIDATE CONSTRAINT page_files_one_owner;
