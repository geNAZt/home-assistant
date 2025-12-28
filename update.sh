#!/bin/bash

# Check if yq is installed
if ! command -v yq &> /dev/null; then
    apk add yq
fi

# Check if yq is installed
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
if [ -z "$ID_TOKEN" ]; then
    echo "No ID token found"
    exit 1
fi

# Parse the ID token
ID_TOKEN=$(printf '%s' "$ID_TOKEN" | jq -r '.id_token')
if [ -z "$ID_TOKEN" ]; then
    echo "No ID token found"
    exit 1
fi

HEADER=$(printf '%s' "$ID_TOKEN" | cut -d '.' -f 1)
PAYLOAD=$(printf '%s' "$ID_TOKEN" | cut -d '.' -f 2)
SIGNATURE=$(printf '%s' "$ID_TOKEN" | cut -d '.' -f 3)

HEADER_JSON="$(printf '%s' "$HEADER" \
  | tr '_-' '/+' \
  | awk '{p=$0; r=length(p)%4; if(r==2)p=p"=="; else if(r==3)p=p"="; print p}' \
  | base64 -d)"

JSON="$(printf '%s' "$PAYLOAD" \
  | tr '_-' '/+' \
  | awk '{p=$0; r=length(p)%4; if(r==2)p=p"=="; else if(r==3)p=p"="; print p}' \
  | base64 -d)"

SIG="$(printf '%s' "$SIGNATURE" \
  | tr '_-' '/+' \
  | awk '{p=$0; r=length(p)%4; if(r==2)p=p"=="; else if(r==3)p=p"="; print p}' \
  | base64 -d)"

# Get the middle part of the ID token to get the issuer
ISSUER=$(printf '%s' "$JSON" | jq -r '.iss')
if [ -z "$ISSUER" ]; then
    echo "No issuer found"
    exit 1
fi

# Get the KID
KID=$(printf '%s' "$HEADER_JSON" | jq -r '.kid')
if [ -z "$KID" ]; then
    echo "No KID found"
    exit 1
fi

PUBLIC_KEY_FILE="/tmp/public_key_${KID}.pem"
rm -rf $PUBLIC_KEY_FILE
if [ ! -f "$PUBLIC_KEY_FILE" ]; then
    # Download the correct public key
    JWKS_URL="${ISSUER}/.well-known/jwks"
    JWKS_CONTENT=$(curl -s $JWKS_URL)
    if [ -z "$JWKS_CONTENT" ]; then
        echo "No JWKS content found"
        exit 1
    fi

    # Get the public key
    PUBLIC_KEY=$(printf '%s' "$JWKS_CONTENT" | jq -r '.keys[] | select(.kid == "'${KID}'") | .x5c[0]')
    if [ -z "$PUBLIC_KEY" ]; then
        echo "No public key found"
        exit 1
    fi


    printf '%s' "$PUBLIC_KEY" | base64 -d > /tmp/cert_${KID}.cer
    openssl x509 -pubkey -noout -in /tmp/cert_${KID}.cer 
    openssl x509 -pubkey -noout -in /tmp/cert_${KID}.cer > /tmp/public_key_${KID}.pem
fi

# Get the public key in PEM format
echo "$PAYLOAD" > /tmp/payload.b64
echo "$SIG" > /tmp/signature.dat

VERIFY_COMPLETE=$(openssl dgst -sha256 -verify /tmp/public_key_${KID}.pem -signature /tmp/signature.dat /tmp/payload.b64)
if [ -z "$VERIFY_COMPLETE" ]; then
    echo "Verification failed"
    exit 1
fi

printf '%s' "$VERIFY_COMPLETE"

SUBJECT=$(printf '%s' "$JSON" | jq -r '.sub')
if [ -z "$SUBJECT" ]; then
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

# We need to sync with the remote repository
git config pull.rebase true
git config rebase.autoStash true
git pull

# Check if working directory is clean
if [ ! -z "$(git status --porcelain)" ]; then
    git add .
    git commit -m "Updates from Home Assistant"
    git push
fi