import traceback
try:
    from workers.tasks.cache import populate_cache
    print("Import OK, task name:", populate_cache.name)
    result = populate_cache.delay("testhash123", '{"test":1}')
    print("Task enqueued! ID:", result.id)
except Exception as e:
    print("ERROR:", e)
    traceback.print_exc()
