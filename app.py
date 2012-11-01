
"""
Copyright (c) 2012 Digital District
----------------------------------------------------

App that creates dailies from inside of Shotgun.

"""

from tank.platform import Application
import tank
import sys
import os
import re
from tank.template import read_templates
import time
import platform
import errno
import tempfile
import urllib
import subprocess
import traceback
import multiprocessing

class DDDailymaker( Application ):

	def init_app( self ):
		self.log_debug("Loading DDDailymaker")
		deny_permissions = self.get_setting("deny_permissions")
		deny_platforms = self.get_setting("deny_platforms")
		p = {
			"title": "Submit a daily",
			"entity_types": ["TankPublishedFile"],
			"deny_permissions": deny_permissions,
			"deny_platforms": deny_platforms,
			"supports_multiple_selection": True
		}
		self.engine.register_command("submit_daily", self._submit_daily, p)
		
	def destroy_app(self):
		self.log_debug("Unloading DDTk-shotgun-dailymaker")
	
	def _submit_daily( self, entity_type, entity_ids ) :
		if entity_type != "TankPublishedFile":
			raise Exception("Action only allows entity_type='TankPublishedFile'.")

		# Retrieve settings
		ttypes = self.get_setting("tank_published_types")
		width = self.get_setting( "width")
		movie_template = self.get_template( "movie_template" )
		system = platform.system()
		try:
			rvio_setting = {"Linux": "rvio_path_linux", 
							"Darwin": "rvio_path_mac", 
						"Windows": "rvio_path_windows"}[system]
			rvio_path = self.get_setting(rvio_setting)
			if not rvio_setting: raise KeyError()
		except KeyError:
			raise Exception("Platform '%s' is not supported." % system) 
		codec_setting = { "Linux": "codec_linux", 
							"Darwin": "codec_mac", 
						"Windows": "codec_windows"}[system]
		codec = self.get_setting(codec_setting)
		# Forge rvls path from rvio, should resides in the same directory
		rvls_path = re.sub( 'rvio', 'rvls', rvio_path)
		for publish_id in entity_ids :
			try :
				thumb_path = None      
			    # Retrieve the entity and some attributes
				d = self.shotgun.find_one("TankPublishedFile", [["id", "is", publish_id]], ["code", "path", "tank_type", "version_number", "downstream_tank_published_files", "image", "entity", "task"])
				path_on_disk = d.get("path").get("local_path")
				name = d.get('code')
				n = self.Notifier( self, "Creating daily for %s" % name, system )
				# Retrieve the template for the images
				img_templ = self.tank.template_from_path( path_on_disk )
				if not img_templ :
					raise RuntimeError( "Couldn't retrieve a template for path %s, root path [%s], skipping it" % ( path_on_disk, self.tank.project_path ))
				type_name = d.get('tank_type').get('name')
				if type_name not in ttypes :
					n.update( "warning", "Skipping %s with unsupported type  : %s" % ( name, type_name ) )
				else :
					# Download the thumbnail in a temp location
					th = urllib.urlopen( d.get('image') )
					( thumbf, thumb_path ) = tempfile.mkstemp()
					os.write( thumbf, th.read() )
					os.close( thumbf )
					# Retrieve images frame range using rvls
					# Replace @ or # by ? so rvls will give us the frame ranges
					start = None
					end = None
					# handle the case where digit are expressed with %d, ? or #
					m = re.search( '\.%(\d+)d\.', path_on_disk)
					if m :
						gm = re.sub( '\.%(\d+)d\.', '.%s.' % ('?' * int( m.group(1))), path_on_disk)
					else : # Assume @ or #
						def repl( m ) :
							return '.%s.' % ( '?' * len( m.group(1) ))
						gm = re.sub( '\.([@|#]+)\.', repl, path_on_disk)
					
					# Problems on Mac OsX with Popen and args as a list ...
					p = subprocess.Popen( ' '.join( [rvls_path, gm] ), stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
					(stdoutdata, stderrdata) = p.communicate()
					ret = p.wait()
					if ret != 0 :
						n.update( "warning", "Couldn't retrieve frame range for %s" % name, append=True)
					else :
						for l in stdoutdata.split() :
							m = re.search( '\.(\d+)-(\d+)[@|#]', l )
							if m :
								start = int( m.group(1) )
								end = int( m.group(2) )
								break
					# Generate a quicktime if not already one
					published_movie = None
					for dp in d.get( "downstream_tank_published_files" ) :
						dpe = self.shotgun.find_one( "TankPublishedFile", [["id", "is", dp['id']]], ["tank_type", "path"])
						if dpe.get('tank_type').get('name') == 'Movie' :
							published_movie = dpe
							break

					if published_movie is None : # Need to generate and create it
						# Extract a base name without any '.' in it and append ".mov"
						movie_name = re.sub( '^([^\.]+).*', '\g<1>', name ) + ".mov"
						n.update( "working", "Generating movie %s for %s" % ( movie_name, name ) )
						# Extract field values from the image template
						fields = img_templ.get_fields(path_on_disk)
						movie_path = movie_template.apply_fields(fields)
						movie_ctx = self.tank.context_from_path( movie_path )
						# Check if there is already a movie with this path, if so, report an error
						if os.path.exists( movie_path ) :
							raise RuntimeError( "There is already a movie named %s but not linked to %s" % ( movie_path, name ) )
						# Create the directory if is does not exist
						movie_dir = os.path.dirname( movie_path )
						try :
							os.makedirs( movie_dir )
						except OSError, e :
							if e.errno != errno.EEXIST : # Check if it fails only because the directory already exists
								raise RuntimeError( "Couldn't create %s : %s" % ( movie_dir, e ) )
						n.update( 'working', 'Generationg movie in the background %s' % movie_path )
						if True :
							proc = multiprocessing.Process( target=DDDailymaker.make_movie, args=(path_on_disk, movie_path, rvio_path, width, codec ) )
							proc.daemon = True
							proc.start()
						else :
							DDDailymaker.make_movie( path_on_disk, movie_path, rvio_path, width, codec )
#						proc = subprocess.Popen( cmd )
						n.update( 'working', 'Building thumbnail ...' )
						# Publish the quicktime, with an upstream dependency to the images
						n.update( "working", "Publishing movie %s" % movie_name )
						published_movie = tank.util.register_publish( self.tank, movie_ctx , movie_path, movie_name, fields['version'],  tank_type='Movie', thumbnail_path=thumb_path, dependency_paths = [path_on_disk] )
					# Create the version
					n.update( "working", "Publishing daily for %s" % name )
					
					data = {
						"code": name,
						"description": "",
						"project": self.context.project,
						"entity": d.get('entity'),
						"sg_task": d.get('task'),
						"created_by": tank.util.get_shotgun_user(self.tank.shotgun),
						"user": tank.util.get_shotgun_user(self.tank.shotgun),
						"sg_path_to_movie": published_movie.get('path').get('local_path'),
						"sg_uploaded_movie" : { 
												"local_path" : published_movie.get('path').get('local_path'),
												"name" : published_movie.get('path').get('name'),
												"link_type" : "local",
												},
						"sg_path_to_frames" : path_on_disk,
						"sg_uploaded_frames" : {
												"local_path" : path_on_disk,
												"name" : name,
												"link_type" : "local",
												},
						"sg_first_frame": start,
						"sg_last_frame": end,
						"frame_count": (end - start) + 1 if end is not None and start is not None else None,
						"frame_range": "%d-%d" % (start, end) if end is not None and start is not None else None,
						"sg_movie_has_slate": False,
						"tank_published_file" : d
					}
					ve = self.shotgun.create("Version", data)
					# and thumbnail
					if thumb_path :
						self.shotgun.upload_thumbnail("Version", ve["id"], thumb_path )
						os.unlink( thumb_path )
					# and filmstrip
	#				if filmstrip:
	#					self.shotgun.upload_filmstrip_thumbnail("Version", ve["id"], filmstrip)
					# Yeah, we did it !
					n.update( "success", "Daily for %s created !" % name )
			except Exception, e :
				n.update( 'error', '%s' % e, append=True )

	class Notifier( ) :
		'''Helper class to report progress and info to the end user'''
		def __init__( self, app, title, system ) :
			self._notifier = None
			self._app = app
			self.update = self._defaultUpdate
			# Retrieve icons path from the app
			location = self._app.disk_location
			self._icons_path = os.path.join( location, 'resources' )
			# Fail gracely if the module can't be found
			# In that case app.log_info will be used to display messages
			if system == 'Linux' :
				try : # Gnome notify
					import pynotify
					self._notifier = pynotify.Notification(title, "", os.path.join( self._icons_path, 'info.png') )
					self._notifier.set_timeout( pynotify.EXPIRES_NEVER )
					self.update = self._gnomeUpdate
					self._notifier.show()
				except Exception, e:
					self._app.log_debug( "%s" % e )
					pass
			elif system == 'Darwin' : # Try Moutain Lion notification center
				try :
					import Foundation
					import objc
					import AppKit
					NSUserNotification = objc.lookUpClass('NSUserNotification')
					self._NSUserNotificationCenter = objc.lookUpClass('NSUserNotificationCenter')
					self._notifier = NSUserNotification.alloc().init()
					self._notifier.setTitle_(str(title))
					#self._notifier.setInformativeText_(str(text))
					#self._notifier.setSoundName_("NSUserNotificationDefaultSoundName")
					#self._notifier.setUserInfo_({"action":"open_url", "value":url})
					#AppKit.NSApplication.sharedApplication().setDelegate_(self)
					self.update = self._macMountainUpdate
					NSUserNotificationCenter.defaultUserNotificationCenter().scheduleNotification_(self._notifier)

				except Exception, e:
					self._app.log_debug( "%s" % e )
					pass
					

		def _defaultUpdate( self, status, message, append=False  ) :
			'''Default notifier updater : use app log_info'''
			self._app.log_info( "%s : %s" % ( status, message ) )

		def _gnomeUpdate( self, status, message, append=False ) :
			'''Update a Gnome notifier'''
			if self._notifier :
				if append :
					self._notifier.set_property( "body", "%s\n%s" % ( self._notifier.get_property( "body" ), message ))
				else : 
					self._notifier.set_property( "body", message )
				self._notifier.set_property( "icon-name", os.path.join( self._icons_path, '%s.png' % status ) )
				self._notifier.show()
			else :
				self._app.log_info( "%s : %s" % ( status, message ) )

		def _macMountainUpdate( self, status, message, append=False ) :
			'''Update Osx notification center'''
			if self._notifier :
				self._notifier.setInformativeText_(str(message))
				# Changing icon does not seem to be possible ?
				#self._notifier.set_property( "icon-name", os.path.join( self._icons_path, '%s.png' % status ) )
				if not append : #Remove previous notifications if not in append mode
					for d in self._NSUserNotificationCenter.defaultUserNotificationCenter().deliveredNotifications() :
						if self._notifier == d :
							self._NSUserNotificationCenter.defaultUserNotificationCenter().removeDeliveredNotification_(d)
				# Push the new one out
				self._NSUserNotificationCenter.defaultUserNotificationCenter().scheduleNotification_(self._notifier)
			else :
				self._app.log_info( "%s : %s" % ( status, message ) )
	
	@staticmethod
	def make_movie( images_path, movie_path, rvio_path, width, codec=None ) :
		'''Generate a movie with rvio'''
		# Create the directory is does not exist
		movie_dir = os.path.dirname( movie_path )
		try :
			os.makedirs( movie_dir )
		except OSError, e :
			if e.errno != errno.EEXIST :
				raise RuntimeError( "Couldn't create %s : %s" % ( movie_dir, e ) )
		cmd = [rvio_path]
		cmd.append( images_path )
		cmd.append( "-resize %d 0" % ( width ) )
		if codec :
			cmd.append( "-codec %s"  % codec )
		cmd.append( " -overlay frameburn .4 1.0 50.0" )
		cmd.append( " -o %s" % ( movie_path ) )
		os.system( ' '.join( cmd ) )


