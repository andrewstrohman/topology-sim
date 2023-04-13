#!/bin/bash

for host in $(cat hardware.yaml | grep host | awk '{print $2}'); do
	if ! ping -c 1 $host > /dev/null; then
		echo $host
	fi
done
