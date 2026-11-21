map.json.gz is the union of two coverage runs (Python 3.10 and Python 3.13 with requirements.lock).
The union only ever adds tests, so it is the safer choice. Import-time lines are likewise unioned.
