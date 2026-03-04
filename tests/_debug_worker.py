"""
Debug script: Run this INSIDE the worker container to simulate exactly
what Celery does when it executes populate_cache.
"""
import sys
sys.path.insert(0, '/app')
import os

print("REDIS_URL_MAB:", os.environ.get("REDIS_URL_MAB", "NOT SET"))
print("REDIS_URL_CELERY:", os.environ.get("REDIS_URL_CELERY", "NOT SET"))

import hashlib

# Simulate EXACTLY what Celery's prefork worker does
try:
    from workers.tasks.cache import populate_cache
    print("Import OK")

    h = hashlib.sha256(b'Is Asia the biggest continent?').hexdigest()
    print("Hash:", h, "len:", len(h))

    # Call the underlying function directly (not via delay)
    # This is what the Celery worker's subprocess actually calls
    # The 'self' parameter is the task instance when bind=True
    result = populate_cache(h, '{"output_text":"Yes, Asia is the biggest continent","provider":"groq","model":"test","token_count":5}')
    print("Task function returned:", result)

except Exception as e:
    import traceback
    print("ERROR:", type(e).__name__, str(e))
    traceback.print_exc()

# Now check Redis
import redis as redis_sync
r = redis_sync.from_url(os.environ.get("REDIS_URL_MAB", "redis://redis:6379/0"), decode_responses=True)
keys = r.keys("cache:*")
print("Redis cache keys after task:", keys)
r.close()
