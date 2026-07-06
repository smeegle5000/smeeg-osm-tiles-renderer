# smeeg-osm-tiles-renderer
all in one script to setup a blank (or live) fedora 44 instance to render openstreetmap tiles with mapnik

vibe coded but it works

just chmod u+x and run, it does everything for you

tiles.py is embedded into the setup.sh, and is written into the correct location for you automatically, no need to download it seperately, only here in case you somehow already have mapnik setup, as it has a nice gui for selecting tiles to render

tested and confirmed working on a fresh fedora 44 live usb enviroment as of the time of posting (when given enough disk space with ``sudo mount -o remount,size=40G /run`` (40gb works but is cutting it close, you may be able to get away with less if you provide a smaller pbf, i used australia oceana)), get the input file from <https://download.geofabrik.de/>
if you dont have enough ram to get a 40gb disk you will need to either install fedora onto a spare hdd, find another place to put the postgresdb, or find another solution
