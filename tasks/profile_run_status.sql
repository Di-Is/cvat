\pset pager off
\timing on

-- Optional flags
\if :{?include_legacy}
\else
    \set include_legacy 1
\endif

-- Set this before running (example: \set run_id '32f42fca-a5a7-45ff-af62-7a38c2d39263')
\if :{?run_id}
\else
    \echo 'WARNING: run_id is not set. Use \set run_id ''<uuid>'' before executing.'
\endif

\echo ''
\if :include_legacy
\echo '=== Legacy AnnotationRequest filters for run_id=' :run_id ' ==='
EXPLAIN (ANALYZE, BUFFERS, TIMING)
SELECT COUNT(*) FROM functions_annotationrequest
WHERE parameters ->> 'function_run_id' = :'run_id';

EXPLAIN (ANALYZE, BUFFERS, TIMING)
SELECT COUNT(*) FROM functions_annotationrequest
WHERE parameters ->> 'function_run_id' = :'run_id'
  AND status = 'done';

EXPLAIN (ANALYZE, BUFFERS, TIMING)
SELECT *
FROM functions_annotationrequest
WHERE parameters ->> 'function_run_id' = :'run_id'
ORDER BY created_at
LIMIT 1;

EXPLAIN (ANALYZE, BUFFERS, TIMING)
SELECT *
FROM functions_annotationrequest
WHERE parameters ->> 'function_run_id' = :'run_id'
  AND status IN ('failed', 'cancelled')
ORDER BY updated_at DESC
LIMIT 1;
\else
\echo '=== Legacy AnnotationRequest filters skipped (include_legacy=0) ==='
\endif

\echo ''
\echo '=== Proposed summary-table lookup ==='
EXPLAIN (ANALYZE, BUFFERS, TIMING)
SELECT run_id, status, progress, total_requests, completed_requests,
       failed_requests, cancelled_requests, active_request_id,
       expected_frames, completed_frames
FROM functions_functionrunstatus
WHERE run_id = :'run_id';

\echo ''
\echo '=== pg_stat_statements snippet (top 5 related to functions_runstatus) ==='
SELECT query, calls, mean_exec_time, max_exec_time, rows
FROM pg_stat_statements
WHERE query ILIKE '%functions_runstatus%'
   OR query ILIKE '%functionrunstatus%'
ORDER BY mean_exec_time DESC
LIMIT 5;
