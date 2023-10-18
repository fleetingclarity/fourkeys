/*
 * Copyright (c) 2023. fleetingclarity <fleetingclarity@proton.me>
 */

-- Tables
CREATE TABLE events_raw (
    id VARCHAR(100) NOT NULL PRIMARY KEY,
    event_type VARCHAR(50) NOT NULL,
    metadata JSONB,
    time_created TIMESTAMP NOT NULL,
    signature VARCHAR(255),
    msg_id BIGINT,
    source VARCHAR(50)
);

CREATE INDEX idx_er_signature ON events_raw(signature);

CREATE TABLE events_enriched (
    events_raw_signature VARCHAR(255) NOT NULL PRIMARY KEY,
    enriched_metadata JSONB
);

-- Views that will be used by the front end for querying
CREATE OR REPLACE VIEW changes AS
SELECT
    source,
    event_type,
    commit->>'id' AS change_id,
    date_trunc('second', (commit->>'timestamp')::timestamp) AS time_created
FROM events_raw e,
LATERAL jsonb_array_elements(e.metadata::jsonb->'commits') AS commit
WHERE event_type = 'push'
GROUP BY 1, 2, 3, 4;
-- end view changes

CREATE OR REPLACE VIEW events AS
SELECT
    raw.id,
    raw.event_type,
    raw.time_created,
    raw.metadata,
    enr.enriched_metadata,
    raw.signature,
    raw.msg_id,
    raw.source
FROM events_raw raw
JOIN events_enriched enr ON raw.signature = enr.events_raw_signature;
-- end view events

CREATE OR REPLACE VIEW deployments AS
with deploys_cloudbuild_github_gitlab as (
    select
        source,
        id as deploy_id,
        time_created,
        case
            when source = 'cloud_build' then metadata#>>'{substitutions, commit_sha}'
            when source like 'github%' then metadata#>>'{deployment, sha}'
            when source like 'gitlab%' then coalesce(
                metadata#>>'{commit, id}',
                substring(metadata->>'commit_url' from '.*commit\/(.*)')
            )
            when source = 'argocd' then metadata#>>'{commit_sha}'
        end as main_commit,
        case
            when source like 'github%' then array(
                select value->>'id'
                from jsonb_array_elements(metadata::jsonb->'commits') as value
            )
            else array[]::text[]
        end as additional_commits
    from events_raw
    where (
        (source = 'cloud_build' and metadata#>>'{status}' = 'success')
        or (source like 'github%' and event_type = 'deployment_status' and metadata#>>'{deployment_status,state}' = 'success')
        or (source like 'gitlab%' and event_type = 'pipeline' and metadata#>>'{object_attributes, status}' = 'success')
        or (source like 'gitlab%' and event_type = 'deployment' and metadata#>>'{status}' = 'success')
        or (source = 'argocd' and metadata#>>'{status}' = 'success')
    )
),
deploys_tekton as (
    select
        source,
        id as deploy_id,
        time_created,
        param->>'value' as main_commit,
        array[]::text[] as additional_commits
    from events_raw,
    lateral jsonb_array_elements(metadata::jsonb->'data'->'pipelinerun'->'spec'->'params') as param
    where event_type = 'dev.tekton.event.pipelinerun.successful.v1'
    and metadata::text like '%gitrevision%'
    and param->>'name' = 'gitrevision'
),
deploys_circleci as (
    select
        source,
        id as deploy_id,
        time_created,
        metadata#>>'{pipeline, vcs, revision}' as main_commit,
        array[]::text[] as additional_commits
    from events_raw
    where source = 'circleci' and event_type = 'workflow-completed' and metadata#>>'{workflow, name}' like '%deploy%' and metadata#>>'{workflow, status}' = 'success'
),
deploys as (
    select * from deploys_cloudbuild_github_gitlab
    union all
    select * from deploys_tekton
    union all
    select * from deploys_circleci
),
changes_raw as (
    select
        id,
        metadata as change_metadata
    from events_raw
),
deployment_changes AS (
    SELECT
        source,
        deploy_id,
        deploys.time_created,
        change_metadata,
        ARRAY(
            SELECT jsonb_array_elements_text(change_metadata#>'{commits, id}')
        ) AS array_commits,
        main_commit
    FROM deploys
    JOIN changes_raw ON (
        changes_raw.id = deploys.main_commit
        OR changes_raw.id = ANY(deploys.additional_commits)
    )
)
SELECT
    source,
    deploy_id,
    time_created,
    main_commit,
    ARRAY_AGG(DISTINCT array_commits) filter(where array_commits <> '{}') AS changes
FROM deployment_changes
GROUP BY 1,2,3,4;
-- end deployments view

CREATE OR REPLACE VIEW incidents AS
SELECT
    source,
    incident_id,
    MIN(LEAST(issue.time_created, COALESCE(root.time_created, issue.time_created))) AS time_created,
    MAX(time_resolved) AS time_resolved,
    ARRAY_AGG(DISTINCT issue.root_cause) FILTER (WHERE issue.root_cause IS NOT NULL) AS changes
FROM
(
    SELECT
        source,
        CASE
            WHEN source LIKE 'github%' THEN metadata#>>'{issue,number}'
            WHEN source LIKE 'gitlab%' AND event_type = 'note' THEN metadata#>>'{object_attributes,noteable_id}'
            WHEN source LIKE 'gitlab%' AND event_type = 'issue' THEN metadata#>>'{object_attributes,id}'
            WHEN source LIKE 'pagerduty%' THEN metadata#>>'{event,data,id}'
        END AS incident_id,
        CASE
            WHEN source LIKE 'github%' THEN (metadata#>>'{issue,created_at}')::timestamp
            WHEN source LIKE 'gitlab%' THEN (metadata#>>'{object_attributes,created_at}')::timestamp
            WHEN source LIKE 'pagerduty%' THEN (metadata#>>'{event,occurred_at}')::timestamp
        END AS time_created,
        CASE
            WHEN source LIKE 'github%' THEN (metadata#>>'{issue,closed_at}')::timestamp
            WHEN source LIKE 'gitlab%' THEN (metadata#>>'{object_attributes,closed_at}')::timestamp
            WHEN source LIKE 'pagerduty%' THEN (metadata#>>'{event,occurred_at}')::timestamp
        END AS time_resolved,
        SUBSTRING(metadata::text FROM 'root cause: ([[:alnum:]]*)') AS root_cause,
        CASE
            WHEN source LIKE 'github%' THEN metadata#>>'{issue,labels}' LIKE '%name%' AND metadata#>>'{issue,labels}' LIKE '%"name":_"Incident"%'
            WHEN source LIKE 'gitlab%' THEN metadata#>'{object_attributes,labels}' ? 'title' AND metadata#>>'{object_attributes,labels}' LIKE '%"title":_"Incident"%'
            WHEN source LIKE 'pagerduty%' THEN TRUE
        END AS bug
    FROM events_raw
    WHERE event_type LIKE 'issue%' OR event_type LIKE 'incident%' OR (event_type = 'note' AND metadata#>>'{object_attributes,noteable_type}' = 'Issue')
) issue
LEFT JOIN (
    SELECT time_created, unnest(changes) AS root_cause_from_deploy
    FROM deployments
) root ON root.root_cause_from_deploy = issue.root_cause
GROUP BY 1, 2
HAVING BOOL_OR(bug) IS TRUE;
-- end incidents view
