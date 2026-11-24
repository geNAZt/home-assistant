#!/bin/bash
set -eo pipefail

# Check if yq is installed
if ! command -v yq &> /dev/null; then
    apk add yq
fi

# Check if jq is installed
if ! command -v jq &> /dev/null; then
    apk add jq
fi

# Check if openssl is installed
if ! command -v openssl &> /dev/null; then
    apk add openssl
fi

# Exchange github token for id token
# We need to read the last line of the id_token.txt file
ID_TOKEN="$(awk 'NF{last=$0} END{print last}' /config/file_notifications/id_token.txt)"
if [ -z "$ID_TOKEN" ] || [ "$ID_TOKEN" == "null" ]; then
    echo "No ID token found"
    exit 1
fi

# Parse the ID token
ID_TOKEN=$(printf '%s' "$ID_TOKEN" | jq -r '.id_token')
if [ -z "$ID_TOKEN" ] || [ "$ID_TOKEN" == "null" ]; then
    echo "No ID token found"
    exit 1
fi

HEADER=$(printf '%s' "$ID_TOKEN" | cut -d '.' -f 1)
PAYLOAD=$(printf '%s' "$ID_TOKEN" | cut -d '.' -f 2)
SIGNATURE=$(printf '%s' "$ID_TOKEN" | cut -d '.' -f 3)

SIGNING_INPUT_FILE=/tmp/jwt_signing_input.txt
SIG_FILE=/tmp/jwt_signature.bin
printf '%s.%s' "$HEADER" "$PAYLOAD" > "$SIGNING_INPUT_FILE"

HEADER_JSON="$(printf '%s' "$HEADER" \
  | tr '_-' '/+' \
  | awk '{p=$0; r=length(p)%4; if(r==2)p=p"=="; else if(r==3)p=p"="; print p}' \
  | base64 -d)"

JSON="$(printf '%s' "$PAYLOAD" \
  | tr '_-' '/+' \
  | awk '{p=$0; r=length(p)%4; if(r==2)p=p"=="; else if(r==3)p=p"="; print p}' \
  | base64 -d)"

printf '%s' "$SIGNATURE" \
  | tr '_-' '/+' \
  | awk '{p=$0; r=length(p)%4; if(r==2)p=p"=="; else if(r==3)p=p"="; print p}' \
  | base64 -d > "$SIG_FILE"

# Get the middle part of the ID token to get the issuer
ISSUER=$(printf '%s' "$JSON" | jq -r '.iss')
if [ "$ISSUER" != "https://token.actions.githubusercontent.com" ]; then
    echo "Invalid issuer: $ISSUER"
    exit 1
fi

# Get the KID (validate alphanumeric / safe characters only)
KID=$(printf '%s' "$HEADER_JSON" | jq -r '.kid')
if [ -z "$KID" ] || [ "$KID" == "null" ] || [[ ! "$KID" =~ ^[a-zA-Z0-9_-]+$ ]]; then
    echo "Invalid or missing KID"
    exit 1
fi

PUBLIC_KEY_FILE="/tmp/public_key_${KID}.pem"
rm -rf "$PUBLIC_KEY_FILE"
if [ ! -f "$PUBLIC_KEY_FILE" ]; then
    # Download the correct public key from GitHub Actions OIDC JWKS
    JWKS_URL="https://token.actions.githubusercontent.com/.well-known/jwks"
    JWKS_CONTENT=$(curl -sf "$JWKS_URL")
    if [ -z "$JWKS_CONTENT" ]; then
        echo "No JWKS content found"
        exit 1
    fi

    # Get the public key safely using jq --arg
    PUBLIC_KEY=$(printf '%s' "$JWKS_CONTENT" | jq -r --arg kid "$KID" '.keys[] | select(.kid == $kid) | .x5c[0]')
    if [ -z "$PUBLIC_KEY" ] || [ "$PUBLIC_KEY" == "null" ]; then
        echo "No public key found"
        exit 1
    fi

    printf '%s' "$PUBLIC_KEY" | base64 -d > "/tmp/cert_${KID}.cer"
    openssl x509 -pubkey -noout -in "/tmp/cert_${KID}.cer"
    openssl x509 -pubkey -noout -in "/tmp/cert_${KID}.cer" > "/tmp/public_key_${KID}.pem"
fi

# Verify JWT signature and require both exit code 0 and "Verified OK"
if ! VERIFY_COMPLETE=$(openssl dgst -sha256 -verify "/tmp/public_key_${KID}.pem" -signature "$SIG_FILE" "$SIGNING_INPUT_FILE" 2>&1); then
    echo "Verification failed: $VERIFY_COMPLETE"
    exit 1
fi

if [ "$VERIFY_COMPLETE" != "Verified OK" ]; then
    echo "Verification failed: $VERIFY_COMPLETE"
    exit 1
fi

printf '%s\n' "$VERIFY_COMPLETE"

# Verify token expiration (exp claim) with 60s clock-skew leeway
EXP=$(printf '%s' "$JSON" | jq -r '.exp // 0')
NOW_TS=$(date +%s)
if [ "$NOW_TS" -gt "$((EXP + 60))" ]; then
    echo "Token has expired (exp=$EXP, now=$NOW_TS)"
    exit 1
fi

SUBJECT=$(printf '%s' "$JSON" | jq -r '.sub')
if [ -z "$SUBJECT" ] || [ "$SUBJECT" == "null" ]; then
    echo "No subject found"
    exit 1
fi

if [ "$SUBJECT" != "repo:geNAZt/home-assistant:ref:refs/heads/main" ]; then
    echo "Subject is not repo:geNAZt/home-assistant:ref:refs/heads/main"
    exit 1
fi

# Check if ~/.ssh exists
if [ ! -d ~/.ssh ]; then
    mkdir -p ~/.ssh
fi

# Check if we have a SSH key loaded
if [ ! -f ~/.ssh/id_ed25519 ]; then 
    yq '.git_ssh_key' /config/secrets.yaml -r > ~/.ssh/id_ed25519
    chmod 600 ~/.ssh/id_ed25519
    ssh-keyscan -H github.com >> ~/.ssh/known_hosts
fi

# Safety: Create backup of untracked data before sync in case git removes anything
if [ -f "./backup_data.sh" ]; then
    ./backup_data.sh || true
fi

BRANCH="main"

# Abort any interrupted rebase/merge state from a previous failed run
git rebase --abort 2>/dev/null || true
git merge --abort 2>/dev/null || true

# Track only the single target branch without tags
git config remote.origin.fetch "+refs/heads/${BRANCH}:refs/remotes/origin/${BRANCH}"
git config fetch.prune true

# Stash local server changes (e.g. HACS updates) before aligning with shallow remote HEAD
STASH_BEFORE="$(git rev-parse -q --verify refs/stash || true)"
git stash push --include-untracked -m "ha-shallow-sync" >/dev/null 2>&1 || true
STASH_AFTER="$(git rev-parse -q --verify refs/stash || true)"

# Fetch only the latest commit (--depth=1) and reset HEAD to it
git fetch --depth=1 --no-tags origin "$BRANCH"
git checkout -f -B "$BRANCH" "origin/$BRANCH"
git reset --hard "origin/$BRANCH"

# Re-apply stashed local server changes if a stash was created
if [ "$STASH_BEFORE" != "$STASH_AFTER" ]; then
    if ! git stash pop >/dev/null 2>&1; then
        # If stash pop aborted before merging (e.g. untracked file collision with origin),
        # apply tracked changes via 3-way patch and restore non-colliding untracked files.
        if [ -z "$(git diff --name-only --diff-filter=U)" ]; then
            git stash show -p refs/stash | git apply --3way 2>/dev/null || true
            if git rev-parse -q --verify 'refs/stash@{0}^3' >/dev/null 2>&1; then
                git ls-tree -r --name-only 'refs/stash@{0}^3' | while IFS= read -r ufile; do
                    if [ -n "$ufile" ] && [ ! -e "$ufile" ]; then
                        git checkout 'refs/stash@{0}^3' -- "$ufile" 2>/dev/null || true
                        git reset HEAD -- "$ufile" >/dev/null 2>&1 || true
                    fi
                done
            fi
        fi

        # Resolve any merge conflicts cleanly in favor of origin/main (HEAD)
        git diff --name-only --diff-filter=U | while IFS= read -r file; do
            [ -z "$file" ] && continue
            if git cat-file -e "HEAD:$file" 2>/dev/null; then
                git checkout --ours -- "$file"
                git add -- "$file"
            else
                git rm -f --cached -- "$file" 2>/dev/null || true
            fi
        done

        git stash drop -q || true
    fi
fi

# Untrack any ignored files that might still be in the index
git rm --cached -r -f --ignore-unmatch \
    '*.pyc' \
    '*.pyo' \
    '*.pyd' \
    '*__pycache__*' \
    'custom_components/ha_washdata/www/*.gz' \
    '*.log' \
    'grid_price_monitor/logs/*' \
    '*.db*' \
    'sfml_stats/*.db*' \
    'sfml_stats/data/*.json' \
    'solar_forecast_eai/*.db*' \
    'toorox_foresight_ha/*.db*' \
    2>/dev/null || true

# Safety: Restore any data files that git reset might have removed
if [ -f "./restore_data.sh" ]; then
    ./restore_data.sh --keep-existing || true
fi

# Commit and push local Home Assistant changes (e.g. HACS updates) if any remain
if [ -n "$(git status --porcelain)" ]; then
    git config user.name >/dev/null 2>&1 || git config user.name "Home Assistant"
    git config user.email >/dev/null 2>&1 || git config user.email "homeassistant@localhost"
    git add .
    if ! git diff --cached --quiet; then
        git commit -m "Updates from Home Assistant"
        git push origin "$BRANCH"
        # Re-shallow to the newly pushed commit so parent commit can also be pruned
        git fetch --depth=1 --no-tags origin "$BRANCH"
    fi
fi

# Purge stashes, tags, extra branches, reflogs, and unreachable history so .git stays at depth=1
git update-ref -d refs/stash 2>/dev/null || true
git update-ref -d ORIG_HEAD 2>/dev/null || true
git tag -l | while IFS= read -r tag; do
    [ -n "$tag" ] && git tag -d "$tag" >/dev/null 2>&1 || true
done
git for-each-ref --format='%(refname)' refs/heads refs/remotes | while IFS= read -r ref; do
    case "$ref" in
        "refs/heads/${BRANCH}"|"refs/remotes/origin/${BRANCH}"|"refs/remotes/origin/HEAD") ;;
        *) git update-ref -d "$ref" 2>/dev/null || true ;;
    esac
done
git reflog expire --expire=now --all
git repack -adq --window=10 --depth=10
git prune --expire=now
