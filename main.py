#!/usr/bin/env python

import re
import sys
import os
import sqlite3
import urllib.parse
import requests
import shutil
from html import unescape

def fatal(msg):
	print(msg, file=sys.stderr)
	sys.exit(1)

class Generator:
	OUTPUT = 'purescript.docset'

	def __init__(self):
		self.assets = set([
			'https://pursuit.purescript.org/static/res/favicon/favicon-16x16.png',
			'https://pursuit.purescript.org/static/res/favicon/favicon-32x32.png'])
		self.package_name = None
		self.version = None

	def generate(self):
		self.create_docset();
		self.create_index()
		self.fetch_builtins()
		packages = self.fetch_index()
		print('Fetching {} packages'.format(len(packages)))
		for package in packages:
			self.fetch_package(package)
		self.db.close()
		self.download_assets()
		self.create_plist()
		print('Done')

	def fetch_index(self):
		self.package_name = None
		self.version = None
		print('Fetching package list')
		r = requests.get('https://pursuit.purescript.org/')
		html = r.text
		packages = re.findall(r'<a href="https://pursuit\.purescript\.org/packages/([^"/]*)">', html)
		self.save_html(html, self.documents_path('index.html'))
		return packages

	def fetch_package(self, package_name):
		self.package_name = package_name
		modules = self.fetch_package_index()
		for module in modules:
			self.fetch_module(module)

	@staticmethod
	def create_docset():
		path = Generator.OUTPUT
		if os.path.exists(path):
			print('Directory "{}" already exists. Delete and continue? (Y/n)'.format(Generator.OUTPUT))
			if input().upper() != 'Y':
				fatal('Aborted')
			shutil.rmtree(path)
		os.makedirs(Generator.documents_path())

	def fetch_package_index(self):
		print('Fetching package {}'.format(self.package_name))
		url = 'https://pursuit.purescript.org/packages/{}'.format(urllib.parse.quote(self.package_name))
		r = requests.get(url)
		if r.status_code != 200:
			fatal('Package "{}" not found'.format(self.package_name))
		self.version = r.url.split('/')[-1]
		modules = re.findall(
			r'<dd class="grouped-list__item"><a href="https://pursuit\.purescript\.org/packages/{}/{}/docs/([^"]+)">'
				.format(re.escape(self.package_name), re.escape(self.version)),
			r.text)
		print('Fetching package {}@{} with {} modules'.format(self.package_name, self.version, len(modules)))
		os.makedirs(self.documents_path(self.package_name, 'docs'))
		self.save_html(r.text, self.documents_path(self.package_name, 'docs', 'index.html'))
		self.cursor.execute(
			'INSERT OR IGNORE INTO searchIndex(name, type, path) VALUES (?,?,?);',
			[self.package_name, 'Package', os.path.join(self.package_name, 'docs', 'index.html')])
		return modules

	def fetch_builtins(self):
		print('Fetching builtins')
		self.package_name = 'builtins'
		self.version = None
		os.makedirs(self.documents_path(self.package_name, 'docs'))
		html = self.fetch_module('Prim')
		modules = re.findall(r'<dd class="grouped-list__item"><a href="\.\./\.\./builtins/docs/(.*?)\.html">', html)
		for module in modules:
			if module != 'Prim':
				self.fetch_module(module)

	def fetch_module(self, module):
		print('Fetching module {}{}/{}'.format(self.package_name, '@' + self.version if self.version else '', module))
		if self.package_name == 'builtins':
			url = 'https://pursuit.purescript.org/builtins/docs/{}'.format(urllib.parse.quote(module))
		else:
			url = 'https://pursuit.purescript.org/packages/{}/{}/docs/{}'\
				.format(urllib.parse.quote(self.package_name), urllib.parse.quote(self.version), urllib.parse.quote(module))
		r = requests.get(url)
		if r.status_code != 200:
			fatal('Module "{}/{}" not found'.format(self.package_name, module))
		html = self.save_html(r.text, self.documents_path(self.package_name, 'docs', urllib.parse.quote(module) + '.html'))
		self.cursor.execute(
			'INSERT OR IGNORE INTO searchIndex(name, type, path) VALUES (?,?,?);',
			[module, 'Module', os.path.join(self.package_name, 'docs', urllib.parse.quote(module) + '.html')])
		self.db.commit()
		return html

	@staticmethod
	def documents_path(*paths):
		return os.path.join(Generator.OUTPUT, 'Contents/Resources/Documents', *paths)

	def save_html(self, html, path):
		if self.package_name:
			prefix = r'../../'
		else:
			prefix = r''
		# remove google font
		html = re.sub(r'<link href="https://fonts\.googleapis\.com/.*?>', '', html)
		# remove widget script
		html = re.sub(r'<script src="https://pursuit.purescript.org/static/widget/.*?>', '', html)
		# remove top banner
		html = re.sub(r'<div class="top-banner clearfix">.*?(<main)', r'\1', html, flags=re.DOTALL)
		# remove searches
		html = re.sub(r'<a href="/search\?.*?">', '<a href="#">', html)
		# replace version selector with actual version
		if self.version:
			html = re.sub(
				r'<select class="version-selector" id="hident2">.*?</select>\s*(<dl class="grouped-list">)',
				r'\1<dt class="grouped-list__title">Version</dt><dd class="grouped-list__item">{}</dd>'.format(self.version),
				html,
				flags=re.DOTALL)
		# collect asset
		assets = re.findall(r'"(https://pursuit\.purescript\.org/static/[^?">]*)', html)
		self.assets |= set(assets)
		html = re.sub(r'https://pursuit\.purescript\.org/(static/[^"?]*)[^">]*', r'{}\1'.format(prefix), html)
		# convert links
		html = re.sub(
			r'https://pursuit\.purescript\.org/(builtins)/docs/([^">#]*)',
			r'{}\1/docs/\2.html'.format(prefix),
			html)
		html = re.sub(
			r'https://pursuit\.purescript\.org/packages/([^/>"]*)(?:/[^/>"]*)?/?([>"])',
			r'{}\1/docs/index.html\2'.format(prefix, self.package_name),
			html)
		html = re.sub(
			r'https://pursuit\.purescript\.org/packages/([^/>"]*)/(?:[^/>"]*)/docs/([^>"#]*)',
			r'{}\1/docs/\2.html'.format(prefix, self.package_name),
			html)
		# find anchors
		html = re.sub(r'<div class="decl" id="(.*?)">', lambda m: self.process_anchor(path, m), html)
		with open(path, 'w') as f:
			f.write(html)
		return html

	def process_anchor(self, path, match):
		type_, name = match.group(1).split(':', maxsplit=1)
		type_ = self.convert_type(type_)
		name = unescape(name)
		anchor_toc = '//apple_ref/cpp/{}/{}'.format(urllib.parse.quote(type_), urllib.parse.quote(name))
		self.cursor.execute(
			'INSERT OR IGNORE INTO searchIndex(name, type, path) VALUES (?,?,?);',
			[name, type_, '{}#{}'.format(os.path.relpath(path, self.documents_path()), anchor_toc)])
		return '{}<a name="{}" class="dashAnchor"></a>'.format(match.group(0), anchor_toc)

	def download_assets(self):
		print('Downloading assets')
		for url in self.assets:
			path = re.match(r'https://pursuit\.purescript\.org/(.*)', url).group(1)
			r = requests.get(url)
			path = self.documents_path(path)
			os.makedirs(os.path.dirname(path), exist_ok=True)
			with open(path, 'wb') as f:
				f.write(r.content)
		os.symlink('Contents/Resources/Documents/static/res/favicon/favicon-16x16.png', self.documents_path('../../../icon.png'))
		os.symlink('Contents/Resources/Documents/static/res/favicon/favicon-32x32.png', self.documents_path('../../../icon@2x.png'))

	@staticmethod
	def create_plist():
		with open('Info.plist.in', 'r') as f:
			plist = f.read()
		with open(Generator.documents_path('../../Info.plist'), 'w') as f:
			f.write(plist)

	@staticmethod
	def convert_type(t):
		TABLE = {
			't': 'Type',
			'v': 'Value',
			'k': 'Kind',
		}
		return TABLE[t] if t in TABLE else t

	def create_index(self):
		self.db = sqlite3.connect(self.documents_path('../docSet.dsidx'))
		self.cursor = self.db.cursor()
		self.cursor.execute('CREATE TABLE searchIndex(id INTEGER PRIMARY KEY, name TEXT, type TEXT, path TEXT);')
		self.cursor.execute('CREATE UNIQUE INDEX anchor ON searchIndex (name, type, path);')

if __name__ == '__main__':
	gen = Generator()
	gen.generate()
