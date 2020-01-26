#!/usr/bin/python3

import argparse		# for parse the args
import configparser # for parsing configuration files
import io
import logging		# for logging
import logging.config
import os			# for log mkdir
import shlex		# for command bulding# for command bulding
import subprocess	# for starting rtl_fm and multimon-ng
import sys			# for py version
import threading
import time			# for time.sleep()


SLEEP_TIME_AFTER_PROC_START = 3


class LogPipe(threading.Thread):
	def __init__(self, logger):
		super().__init__()
		self.daemon = True
		self.__fd_read, self.__fd_write = os.pipe()
		self.__pipeReader = os.fdopen(self.__fd_read)
		self.__logger = logger
		self.start()

	def fileno(self):
		return self.__fd_write

	def run(self):
		for line in iter(self.__pipeReader.readline, ''):
			self.__logger.info(line.strip())
		self.__pipeReader.close()

	def close(self):
		os.close(self.__fd_write)


def parse_args():
	parser = argparse.ArgumentParser(
		prog="boswatch.py", 
		description="BOSWatch is a Python Script to recive and " \
					"decode german BOS information with rtl_fm and multimon-NG", 
		epilog="More options you can find in the extern config.ini file in " \
			   "the folder /config")
	parser.add_argument("--config", 
						default=os.path.abspath(os.path.join(os.path.dirname(__file__), 'config')),
						help="Parse to config file directory")
	parser.add_argument("--plugin",
						default="main",
						help="Plugin which should be started. (Main for main programming)")
	return parser.parse_args()


def parse_config(config_path, plugin):
	"""
	Parse configuration

	@param config_path
		Directory in which the configuration files are stored
	@param plugin
		Plugin which is started
	@return ConfigParser Instance which holds all the configuration
	"""
	config = configparser.ConfigParser()

	config_file = os.path.join(config_path, 'default.ini')
	if os.path.isfile(config_file):
		config.read(config_file)
	else:
		print('Config file not found: {}'.format(config_file))

	config_file = os.path.join(config_path, '{}.ini'.format(plugin.lower()))
	if os.path.isfile(config_file):
		config.read(config_file)
	else:
		print('Config file not found: {}'.format(config_file))

	return config


def setup_listener(logger, config, listener, mm_write):
	logger.info('Setup listener')
	section = 'listener_{}'.format(listener)
	if not config.has_section(section):
		logger.error('Listener is not properly configured')
		return

	device = config.get(section, 'device')
	freq = config.get(section, 'frequency')
	squelch = config.getfloat(section, 'squelch')
	gain = config.getfloat(section, 'gain')
	error = config.getfloat(section, 'error')
	demod = config.get(section, 'demod').split(',')

	rtl_exec = config.get('DEFAULT', 'rtl_path')
	multimon_exec = config.get('DEFAULT', 'multimon_path')

	logger.info('Device: {}'.format(device))
	logger.info('Frequency: {}'.format(freq))
	logger.info('Squelch: {}'.format(squelch))
	logger.info('Gain: {}'.format(gain))
	logger.info('Error: {}'.format(error))
	logger.info('Demodulations: {}'.format(', '.join(demod)))

	command = shlex.join([
		rtl_exec,
		'-d', str(device),
		'-f', str(freq),
		'-M', 'fm',
		'-p', str(error),
		'-E', 'DC',
		'-F', '0',
		'-l', str(squelch),
		'-g', str(gain),
		'-s', '22050'
	])

	logger.info('RTL-FM command: {}'.format(command))

	rtl_logger = LogPipe(logger.getChild('rtlfm'))
	rtl_fm = subprocess.Popen(shlex.split(command), 
							  stdout=subprocess.PIPE,
							  stderr=rtl_logger,
							  shell=False)

	time.sleep(SLEEP_TIME_AFTER_PROC_START)

	if rtl_fm.poll():
		logger.error('RTL FM did exit unexpected')
		raise Exception('RTL FM did exit unexpected')

	command = shlex.join([
		multimon_exec,
		'-a', 'ZVEI1',		# ZVEI
		'-f', 'alpha',
		'-t', 'raw',
		'/dev/stdin', '-'
	])

	logger.info('multimon command: {}'.format(command))

	multimon_logger = LogPipe(logger.getChild('multimon'))
	mm = subprocess.Popen(shlex.split(command), 
						  stdin=rtl_fm.stdout,
						  stdout=mm_write, 
						  stderr=multimon_logger,
						  shell=False)

	time.sleep(SLEEP_TIME_AFTER_PROC_START)

	if mm.poll():
		logger.error('multimon did exit unexpected')
		logger.error('Kill RTL FM')
		rtl_fm.kill()
		raise Exception('multimon did exit unexpected')

	return rtl_fm, mm


def run_main(logger, config):
	listener_names = config.get('DEFAULT', 'listeners').split(',')

	multimon_fd_read, multimon_fd_write = os.pipe()
	multimon_read = os.fdopen(multimon_fd_read)
	multimon_write = os.fdopen(multimon_fd_write)

	rtls = []
	multimons = []

	try:
		for listener_name in listener_names:
			list_logger = logger.getChild(listener_name)
			instances = setup_listener(list_logger, config, listener_name, multimon_write)
			if instances is None:
				continue
			rtl, multimon = instances
			rtls.append(rtl)
			multimons.append(multimon)
			logger.info('Started listener {}'.format(listener_name))
		logger.info('Started all listeners')

		for mmstate in multimon_read.readlines():
			print(mmstate)
			logger.info(mmstate)
	except KeyboardInterrupt:
		pass
	except Exception as e:
		logger.error(str(e))
		raise e
	finally:
		logger.info('Kill listeners')
		for rtl, multimon in zip(rtls, multimons):
			logger.info('Kill RTL {}'.format(rtl.pid))
			rtl.kill()
			logger.info('Kill multimon {}'.format(multimon.pid))
			multimon.kill()


def run_plugin(logger, config, plugin_name):
	import importlib.util
	from importlib import import_module

	filename = os.path.abspath(os.path.join(
		os.path.dirname(__file__), 
		'plugins', 
		plugin_name, 
		'{}.py'.format(plugin_name)))

	spec = importlib.util.spec_from_file_location(plugin_name, filename)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)


if __name__ == '__main__':
	args = parse_args()
	config = parse_config(args.config, args.plugin)

	SLEEP_TIME_AFTER_PROC_START = config.getfloat('DEFAULT', 'processStartupTime')

	logging.config.fileConfig(os.path.join(args.config, 'logging.ini'))

	logger = logging.getLogger('boslog')
	logger.info('Plugin: {}'.format(args.plugin.lower()))

	if args.plugin.lower() == 'main':
		try:
			run_main(logger, config)
		except Exception as e:
			logger.error(str(e))
			raise e
		finally:
			logger.info('Shutdown')
	else:
		logger = logger.getChild('plugin.{}'.format(args.plugin.lower()))
		try:
			run_plugin(logger, config, args.plugin)
		except Exception as e:
			logger.error(str(e))
			raise e
		finally:
			logger.info('Shutdown plugin')

	logger.info('Shutdown complete')

