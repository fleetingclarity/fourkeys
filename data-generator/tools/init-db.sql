CREATE OR REPLACE TABLE events_raw (
    id VARCHAR(100) NOT NULL PRIMARY KEY,
    event_type VARCHAR(50) NOT NULL,
    metadata JSON,
    time_created TIMESTAMP NOT NULL,
    signature VARCHAR(255),
    msg_id BIGINT UNSIGNED,
    source VARCHAR(50),
    INDEX idx_er_signature (signature)
);

CREATE OR REPLACE TABLE events_enriched (
    events_raw_signature VARCHAR(255) NOT NULL,
    enriched_metadata JSON,
    PRIMARY KEY (events_raw_signature)
);

DELIMITER //
CREATE FUNCTION multiFormatParseTimestamp(input VARCHAR(255))
RETURNS DATETIME
BEGIN
    DECLARE result DATETIME;

    -- 2022-01-05 04:36:28 -0800 -or- (...)+0800
    IF input REGEXP '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} [+-][0-9]{4}$' THEN
        SET result = STR_TO_DATE(input, '%Y-%m-%d %H:%i:%s %z');

    -- 2022-01-12T09:47:26.948+01:00 -or- (...)-0100
    ELSEIF input REGEXP '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}[+-][0-9]{2}:[0-9]{2}$' THEN
        SET result = STR_TO_DATE(input, '%Y-%m-%dT%H:%i:%s.%f%z');

    -- 2022-01-18 05:35:35.320020 -or- 2022-01-18 05:35:35
    ELSEIF input REGEXP '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}\\.?[0-9]*$' THEN
        SET result = STR_TO_DATE(input, '%Y-%m-%d %H:%i:%s.%f');

    ELSE
        SET result = NULL; -- or handle this case differently if needed
    END IF;

    RETURN result;
END //
DELIMITER ;

CREATE OR REPLACE VIEW changes AS
SELECT
    source,
    event_type,
    JSON_UNQUOTE(JSON_EXTRACT(commit, '$.id')) AS change_id,
    multiFormatParseTimestamp(JSON_UNQUOTE(JSON_EXTRACT(commit, '$.timestamp'))) AS time_created
FROM (
    SELECT
        source,
        event_type,
        JSON_EXTRACT(metadata, CONCAT('$.commits[', idx, ']')) AS commit
    FROM four_keys.events_raw
    JOIN JSON_TABLE(
        '[0,1,2,3,4,5,6,7,8,9]',
        "$[*]" COLUMNS(
            idx INT PATH "$"
        )
    ) AS jt ON CHAR_LENGTH(metadata) - CHAR_LENGTH(REPLACE(metadata, 'commit', '')) >= idx
    WHERE event_type = 'push'
) AS subquery
WHERE commit IS NOT NULL
GROUP BY 1,2,3,4;

DELIMITER //

CREATE FUNCTION json2array(json TEXT) RETURNS TEXT
BEGIN
    DECLARE idx INT DEFAULT 1;
    DECLARE result TEXT DEFAULT '';
    DECLARE element TEXT;
    DECLARE jsonLength INT;

    SET jsonLength = JSON_LENGTH(json);

    WHILE idx <= jsonLength DO
        SET element = JSON_EXTRACT(json, CONCAT('$[', idx - 1, ']'));
        SET result = CONCAT(result, ',', JSON_QUOTE(element));
        SET idx = idx + 1;
    END WHILE;

    IF LENGTH(result) > 0 THEN
        SET result = CONCAT('[', SUBSTRING(result, 2), ']');  -- Remove the leading comma and wrap in square brackets
    ELSE
        SET result = '[]';
    END IF;

    RETURN result;
END //

DELIMITER ;

CREATE OR REPLACE VIEW deploys_cloudbuild_github_gitlab_view AS
SELECT
    source,
    id as deploy_id,
    time_created,
    CASE
        WHEN source = 'cloud_build' THEN JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.substitutions.COMMIT_SHA'))
        WHEN source LIKE 'github%' THEN JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.deployment.sha'))
        WHEN source LIKE 'gitlab%' THEN COALESCE(
            JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.commit.id')),
            SUBSTRING(JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.commit_url')) FROM '.*/commit/(.*)')
        )
        WHEN source = 'argocd' THEN JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.commit_sha'))
    END AS main_commit,
    CASE
        WHEN source LIKE 'github%' THEN multiFormatParseTimestamp(JSON_EXTRACT(metadata, '$.deployment.additional_sha'))
        ELSE '[]'
    END AS additional_commits
FROM four_keys.events_raw
WHERE (
    (source = 'cloud_build' AND JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.status')) = 'SUCCESS')
    OR (source LIKE 'github%' AND event_type = 'deployment_status' AND JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.deployment_status.state')) = 'success')
    OR (source LIKE 'gitlab%' AND event_type = 'pipeline' AND JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.object_attributes.status')) = 'success')
    OR (source LIKE 'gitlab%' AND event_type = 'deployment' AND JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.status')) = 'success')
    OR (source = 'argocd' AND JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.status')) = 'SUCCESS')
);

CREATE OR REPLACE VIEW deploys_circleci_view AS
SELECT
    source,
    id AS deploy_id,
    time_created,
    JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.pipeline.vcs.revision')) AS main_commit,
    '[]' AS additional_commits
FROM four_keys.events_raw
WHERE (source = 'circleci' AND event_type = 'workflow-completed' AND JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.workflow.name')) LIKE '%deploy%' AND JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.workflow.status')) = 'success');

CREATE OR REPLACE VIEW deploys_tekton_view AS
SELECT
    e.source,
    e.id AS deploy_id,
    e.time_created,
    CASE
        WHEN jt.name = 'gitrevision' THEN jt.value
        ELSE NULL
    END AS main_commit
FROM (
    SELECT
        id,
        time_created,
        source,
        json2array(JSON_EXTRACT(metadata, '$.data.pipelineRun.spec.params')) AS params
    FROM four_keys.events_raw
    WHERE event_type = 'dev.tekton.event.pipelinerun.successful.v1'
      AND metadata LIKE '%gitrevision%'
) AS e
JOIN JSON_TABLE(
    e.params,
    "$[*]" COLUMNS (
        name TEXT PATH '$.name',
        value TEXT PATH '$.value'
    )
) AS jt ON jt.name = 'gitrevision';

-- put the individual system view together into one view
CREATE OR REPLACE VIEW deployments AS
SELECT
    source,
    deploy_id,
    time_created,
    main_commit,
    NULL AS additional_commits
FROM deploys_cloudbuild_github_gitlab_view

UNION ALL

SELECT
    source,
    deploy_id,
    time_created,
    main_commit,
    NULL AS additional_commits
FROM deploys_tekton_view

UNION ALL

SELECT
    source,
    deploy_id,
    time_created,
    main_commit,
    NULL AS additional_commits
FROM deploys_circleci_view;

CREATE OR REPLACE VIEW incidents AS
SELECT
    issue.source,
    issue.incident_id,
    MIN(IF(root.time_created < issue.time_created, root.time_created, issue.time_created)) as time_created,
    MAX(issue.time_resolved) as time_resolved,
    GROUP_CONCAT(DISTINCT root.main_commit) AS changes
FROM
(
    SELECT
        source,
        CASE
            WHEN source LIKE 'github%' THEN JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.issue.number'))
            WHEN source LIKE 'gitlab%' AND event_type = 'note' THEN JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.object_attributes.noteable_id'))
            WHEN source LIKE 'gitlab%' AND event_type = 'issue' THEN JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.object_attributes.id'))
            WHEN source LIKE 'pagerduty%' THEN JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.event.data.id'))
        END AS incident_id,
        multiFormatParseTimestamp(
            CASE
                WHEN source LIKE 'github%' THEN JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.issue.created_at'))
                WHEN source LIKE 'gitlab%' THEN JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.object_attributes.created_at'))
                WHEN source LIKE 'pagerduty%' THEN JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.event.occurred_at'))
            END
        ) AS time_created,
        multiFormatParseTimestamp(
            CASE
                WHEN source LIKE 'github%' THEN JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.issue.closed_at'))
                WHEN source LIKE 'gitlab%' THEN JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.object_attributes.closed_at'))
                WHEN source LIKE 'pagerduty%' THEN JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.event.occurred_at'))
            END
        ) AS time_resolved,
        REGEXP_REPLACE(metadata, '.*root cause:\\s*([^\\s][^\\W]+).*','\\1') as root_cause,
        CASE
            WHEN source LIKE 'github%' THEN JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.issue.labels')) LIKE '%"name":_"Incident"%'
            WHEN source LIKE 'gitlab%' THEN JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.object_attributes.labels')) LIKE '%"title":_"Incident"%'
            WHEN source LIKE 'pagerduty%' THEN TRUE
        END AS bug
    FROM four_keys.events_raw
    WHERE event_type LIKE 'issue%' OR event_type LIKE 'incident%' OR (event_type = 'note' AND JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.object_attributes.noteable_type')) = 'Issue')
) AS issue
LEFT JOIN deployments AS root ON root.main_commit = issue.root_cause
WHERE issue.bug = TRUE
GROUP BY issue.source, issue.incident_id, issue.time_created, issue.time_resolved;

