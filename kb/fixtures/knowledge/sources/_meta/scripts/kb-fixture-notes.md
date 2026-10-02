# Fixture runner notes

The fixture runner writes one record per launch into a dated log.
A launch that finds the lock held waits one minute and tries again.

Records older than 30 days are kept, never trimmed.
