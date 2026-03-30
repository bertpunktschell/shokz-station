#!/bin/bash
# Shokz USB Mount Watcher - runs as systemd service
# Polls every 3s, mounts first USB partition to /media/shokz
# Respects /run/user/1000/shokz-ejected lock file (set by eject button)

MOUNT=/media/shokz
LOCK=/run/user/1000/shokz-ejected
mkdir -p $MOUNT

while true; do
    DEVICE=$(lsblk -rno NAME,PKNAME,TYPE | while read name parent type; do
        if [ "$type" = "part" ] && [ -n "$parent" ]; then
            tran=$(lsblk -rno TRAN /dev/$parent 2>/dev/null)
            if [ "$tran" = "usb" ]; then
                echo /dev/$name
                break
            fi
        fi
    done)

    if [ -n "$DEVICE" ]; then
        # Device present
        if [ -f "$LOCK" ]; then
            : # User ejected - don't remount until replug
        elif ! mountpoint -q $MOUNT; then
            mount $DEVICE $MOUNT -o uid=1000,gid=1000,umask=022 2>/dev/null && \
                logger "shokz-watch: mounted $DEVICE"
        fi
    else
        # Device gone - clear lock and unmount if needed
        rm -f $LOCK
        if mountpoint -q $MOUNT; then
            umount -l $MOUNT 2>/dev/null
        fi
    fi
    sleep 3
done
