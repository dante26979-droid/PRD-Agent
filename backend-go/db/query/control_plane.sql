-- The first repository slice is hand-written to keep transaction boundaries
-- explicit. These queries are the canonical input for the later sqlc cutover.
-- name: Health :one
SELECT 1;
