# InfraForge custom prompt and aliases
# Deployed by: ansible/playbooks/custom-bashrc.yml

# Only apply to interactive shells
[ -z "$PS1" ] && return

# Set color variables
RED='\[\033[01;31m\]'
CYAN='\[\033[01;36m\]'
GREEN='\[\033[01;32m\]'
YELLOW='\[\033[01;33m\]'
BLUE='\[\033[01;34m\]'
PURPLE='\[\033[01;35m\]'
RESET='\[\033[00m\]'

# Customize the prompt based on user
if [ "$(id -u)" -eq 0 ]; then
    PS1="💀 ${RED}\u${RESET}@${YELLOW}\h${RESET}:${BLUE}\w${RESET}# "
else
    PS1="${CYAN}\u${RESET}@${YELLOW}\h${RESET}:${GREEN}\w${RESET}\$ "
fi

export PS1

alias pbcopy='xclip -selection clipboard'
alias pbpaste='xclip -selection clipboard -o'
