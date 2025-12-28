#!/bin/bash

# Exchange github token for id token


# Check if yq is installed
if ! command -v yq &> /dev/null; then
    apk add yq
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