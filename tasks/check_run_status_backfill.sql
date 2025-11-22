\pset pager off
\timing on

\echo ''
\echo '=== AnnotationRequest rows missing run_status_id ==='
SELECT COUNT(*) AS missing_total
FROM functions_annotationrequest
WHERE run_status_id IS NULL;

\echo ''
\echo '=== Breakdown by category ==='
SELECT category, COUNT(*) AS missing
FROM functions_annotationrequest
WHERE run_status_id IS NULL
GROUP BY category
ORDER BY missing DESC;

\echo ''
\echo '=== Top run_ids present in parameters but missing FK ==='
SELECT parameters ->> 'function_run_id' AS run_id,
       COUNT(*) AS request_count,
       MIN(created_at) AS oldest_created_at,
       MAX(updated_at) AS newest_updated_at
FROM functions_annotationrequest
WHERE run_status_id IS NULL
  AND parameters ? 'function_run_id'
GROUP BY run_id
ORDER BY request_count DESC NULLS LAST
LIMIT 20;

\echo ''
\echo '=== Recent rows missing run_status_id (limit 20) ==='
SELECT id, function_id, job_id, category, type, status,
       parameters ->> 'function_run_id' AS run_id,
       created_at, updated_at
FROM functions_annotationrequest
WHERE run_status_id IS NULL
ORDER BY updated_at DESC
LIMIT 20;
