#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

# SMP upgrader: if you have an already running setup run this file to download and apply the latest stable version.
#
#   ./upgrade.sh
#
# 0. Checks if an update is present (Based on global version counter located at server\pyproject.toml) and downloads the latest release from the projects github
#   if you have your own server e.g modified project you can specify the --download-url parameter)
#This process will overrite some files on disk so if you made modifications to the stack present save them in a separate folder before running this script. Note that the data folders are not touched so no user data is lost.
# 1. checks for the existing .env file and builds from that, so no data is changed.
# 2. stops all running containers -> if you have active clients this will forcefully disconnect them.
# 
# 4. rebuilds the docker stack with the new files. since it is working from an already built database at the end of the process no usernames or passwords will be printed.
# Since everything is rebuilt the program recompiles the Windows agent, offered at
#    https://<smp>/downloads/smp-agent.exe
# Currently there is no auto update of agents so for new features to reach end devices so you must update manually. Note this is currently being tracked as issue SMP#12 and will be resolved in v.1.0.3 
# The recompiling might take up to 3 minutes, so expect downtime during this process.
# 5. After the docker stack is running the program will display all changes it made (version counter -> files changed, version notes)

