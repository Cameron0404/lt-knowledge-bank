# The backup routine (EXAMPLE source, invented)

This file is an invented example for the knowledge bank demo. It describes no real system.

## When it runs

The backup job runs at 02:15 every night and copies the notes folder to the external drive.
A run that finds the drive missing writes one line to the morning report and exits with code 3.

## What it keeps

Each night's copy is kept for 14 days, then the oldest copy is overwritten.
The job never deletes the source folder, it only reads it.
