# Logs Directory

This directory contains application logs for the Genesia Prompt Enhancer with automatic rotation.

## Log Files

### Application Logs (Python)
- **Location**: `logs/prompt-enhancer.log`
- **Rotation**: Automatic via RotatingFileHandler
  - Maximum size: 10MB per file
  - Backup files: 5 (prompt-enhancer.log.1 through .5)
  - Oldest log is deleted when limit is reached
- **Enabled by**: Set `LOG_TO_FILE=true` environment variable
- **Format**: `YYYY-MM-DD HH:MM:SS - module - LEVEL - message`

### Server Logs (llama-server)
- **Location**: `../llama-server.log` (project root)
- **Rotation**: Via logrotate (see `../logrotate.conf`)
  - Daily rotation
  - Keep 14 days of logs
  - Maximum size: 100MB before forced rotation
  - Compressed after 1 day (`.gz` files)
  - Date-stamped backups: `llama-server.log-YYYYMMDD`

## Viewing Logs

### Real-time Application Logs
```bash
# If LOG_TO_FILE=true
tail -f logs/prompt-enhancer.log

# Console output (default)
LOG_LEVEL=INFO python local_prompt_enhancer.py "Your prompt"
```

### Real-time Server Logs (systemd service)
```bash
# Option 1: Via systemd journal
sudo journalctl -u prompt-enhancer -f

# Option 2: Via log file
tail -f llama-server.log
```

### Historical Logs
```bash
# Application logs
ls -lh logs/prompt-enhancer.log*

# Server logs (compressed)
ls -lh llama-server.log*
zcat llama-server.log-20260125.gz | less
```

## Log Levels

Control verbosity with the `LOG_LEVEL` environment variable:

- **DEBUG**: Very verbose, includes all operations and state changes
- **INFO**: General information about requests and connections
- **WARNING**: Important events that might need attention (default)
- **ERROR**: Error messages only

### Examples
```bash
# Debug level with file logging
LOG_LEVEL=DEBUG LOG_TO_FILE=true python local_prompt_enhancer.py "test"

# Info level, console only
LOG_LEVEL=INFO python local_prompt_enhancer.py "test"

# Set globally for session
export LOG_LEVEL=INFO
export LOG_TO_FILE=true
```

## Log Rotation Management

### Manual Rotation (testing)
```bash
# Test logrotate configuration
sudo logrotate -f ../logrotate.conf

# Force rotation of application logs
# (Python RotatingFileHandler rotates automatically at 10MB)
```

### Automatic Rotation Setup

**Recommended: Use the installer script**
```bash
# Run the automated installer (recommended)
../install-logrotate.sh

# This will:
# - Substitute paths automatically
# - Install to /etc/logrotate.d/
# - Test the configuration
# - Show preview before installing
```

**Manual installation (advanced)**
```bash
# Manually substitute %INSTALL_DIR% in logrotate.conf, then:
sudo cp /path/to/modified/logrotate.conf /etc/logrotate.d/genesia-prompt-enhancer
sudo chmod 644 /etc/logrotate.d/genesia-prompt-enhancer

# Verify configuration
sudo logrotate -d /etc/logrotate.d/genesia-prompt-enhancer
```

**How it works:**
- Logrotate uses `copytruncate` to copy log contents and truncate the original file
- This allows llama-server to keep writing to the same file descriptor
- No service restart or reload needed during rotation
- Runs automatically via cron (usually daily at 6:25 AM)

### Journal Logging (Alternative)
To use systemd journal instead of file logging:

1. Edit `prompt-enhancer.service`
2. Uncomment journal logging options
3. Reload systemd: `sudo systemctl daemon-reload`
4. Restart service: `sudo systemctl restart prompt-enhancer`

View with: `sudo journalctl -u prompt-enhancer -f`

## Disk Space Management

### Current Usage
```bash
# Check log sizes
du -h logs/
du -h ../llama-server.log*

# Check total project size
du -sh ..
```

### Space Limits
- Application logs: Max ~50MB (10MB × 5 backups)
- Server logs: Max ~1.4GB (100MB × 14 days, compressed)
- Systemd journal: Configured in `/etc/systemd/journald.conf`

### Cleanup
```bash
# Remove old application logs
rm logs/prompt-enhancer.log.[3-5]

# Remove old server logs (older than 7 days)
find . -name "llama-server.log-*" -mtime +7 -delete

# Clear all logs (careful!)
rm -f logs/*.log* ../llama-server.log*
```

## Troubleshooting

### No logs appearing
1. Check log level: `echo $LOG_LEVEL`
2. Enable file logging: `export LOG_TO_FILE=true`
3. Check permissions: `ls -la logs/`
4. Verify service status: `systemctl status prompt-enhancer`

### Disk space issues
1. Check log sizes: `du -h logs/ ../llama-server.log*`
2. Reduce retention: Edit `../logrotate.conf` (reduce `rotate 14` value)
3. Lower size limits: Edit `maxBytes` in `local_prompt_enhancer.py`

### Logrotate not working
1. Verify installation: `sudo ls -la /etc/logrotate.d/genesia*`
2. Test configuration syntax: `sudo logrotate -d /etc/logrotate.d/genesia-prompt-enhancer`
3. Test manually (dry-run): `sudo logrotate -d /etc/logrotate.d/genesia-prompt-enhancer`
4. Force rotation (actual): `sudo logrotate -f /etc/logrotate.d/genesia-prompt-enhancer`
5. Check logrotate status: `sudo cat /var/lib/logrotate/status | grep genesia`
6. Review logrotate errors: `sudo journalctl -u logrotate`

**Verify rotation is working:**
```bash
# Check when files were last rotated
ls -lht ../llama-server.log* logs/*.log*

# Look for dated backups (e.g., llama-server.log-20260126)
ls -1 ../llama-server.log-*

# Check logrotate last run time
sudo grep genesia /var/lib/logrotate/status

# Monitor live (create test logs and force rotate)
echo "Test log entry $(date)" >> ../llama-server.log
sudo logrotate -f /etc/logrotate.d/genesia-prompt-enhancer
ls -lh ../llama-server.log*
```

## Best Practices

1. **Development**: Use `LOG_LEVEL=DEBUG` with console output
2. **Production**: Use `LOG_LEVEL=INFO` with `LOG_TO_FILE=true`
3. **Monitoring**: Use systemd journal for centralized logging
4. **Archival**: Compress and move old logs to backup storage
5. **Privacy**: Logs may contain prompts - secure accordingly

## Notes

- All log files are excluded from git (see `.gitignore`)
- Logrotate runs daily via cron
- Python rotation is automatic when file size exceeds 10MB
- Systemd journal has built-in rotation (see `journald.conf`)
