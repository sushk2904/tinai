import sys
sys.path.insert(0, '/app')

try:
    from workers.tasks.cache import populate_cache
    import hashlib
    h = hashlib.sha256(b'test prompt debug').hexdigest()
    print('Task object:', populate_cache)
    print('Celery broker:', populate_cache.app.conf.broker_url)
    result = populate_cache.delay(h, '{"output_text":"test","provider":"groq","model":"test","token_count":1}')
    print('SUCCESS - task id:', result.id)
except Exception as e:
    import traceback
    print('FAIL:', type(e).__name__, str(e))
    traceback.print_exc()
