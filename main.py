#!/usr/bin/env python

import re
import sys
import os
import sqlite3
import urllib.parse
import requests
import shutil
from html import unescape
from bs4 import BeautifulSoup

def fatal(msg):
	print(msg, file=sys.stderr)
	sys.exit(1)

class URLUtilities:
	INDEX = 'https://pursuit.purescript.org/'
	STATIC = 'https://pursuit.purescript.org/static/'

	@staticmethod
	def package(package):
		return 'https://pursuit.purescript.org/packages/{}'.format(urllib.parse.quote(package, ''))

	@staticmethod
	def module(package, version, module):
		if package == 'builtins':
			return 'https://pursuit.purescript.org/builtins/docs/{}'.format(urllib.parse.quote(module, ''))
		else:
			return 'https://pursuit.purescript.org/packages/{}/{}/docs/{}'\
				.format(urllib.parse.quote(package, ''), urllib.parse.quote(version, ''), urllib.parse.quote(module, ''))

class HTMLUtilities:
	@staticmethod
	def find_packages(html):
		return re.findall(r'<a href="https://pursuit\.purescript\.org/packages/([^"/]*)">', html)

	@staticmethod
	def find_modules(html):
		return re.findall(
			r'<dd class="grouped-list__item"><a href="https://pursuit\.purescript\.org/packages/(?:[^/]+)/(?:[^/]+)/docs/([^"/]+)">',
			html)

	@staticmethod
	def find_modules_builtins(html):
		return re.findall(r'<dd class="grouped-list__item"><a href="https://pursuit\.purescript\.org/builtins/docs/(.*?)">', html)

class Generator:
	OUTPUT = 'purescript.docset'

	def __init__(self):
		self.assets = set([
			'https://pursuit.purescript.org/static/res/favicon/favicon-16x16.png',
			'https://pursuit.purescript.org/static/res/favicon/favicon-32x32.png'])
		self.package = None
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
		self.package = None
		self.version = None
		print('Fetching package list')
		r = requests.get(URLUtilities.INDEX)
		html = re.sub(r'(</a></li>)</li>', r'\1', r.text) # fix html error
		self.save_html(html, self.documents_path('index.html'))
		packages = HTMLUtilities.find_packages(html)
		return packages

	def fetch_package(self, package):
		self.package = package
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
		print('Fetching package {}'.format(self.package))
		r = requests.get(URLUtilities.package(self.package))
		if r.status_code != 200:
			fatal('Package "{}" not found'.format(self.package))
		self.version = r.url.split('/')[-1]
		modules = HTMLUtilities.find_modules(r.text)
		print('Fetching package {}@{} with {} modules'.format(self.package, self.version, len(modules)))
		os.makedirs(self.documents_path(self.package, 'docs'))
		self.save_html(r.text, self.documents_path(self.package, 'docs', 'index.html'))
		self.cursor.execute(
			'INSERT OR IGNORE INTO searchIndex(name, type, path) VALUES (?,?,?);',
			[self.package, 'Package', os.path.join(self.package, 'docs', 'index.html')])
		return modules

	def fetch_builtins(self):
		print('Fetching builtins')
		self.package = 'builtins'
		self.version = None
		os.makedirs(self.documents_path(self.package, 'docs'))
		html = self.fetch_module('Prim')
		modules = HTMLUtilities.find_modules_builtins(html)
		for module in modules:
			if module != 'Prim':
				self.fetch_module(module)

	def fetch_module(self, module):
		print('Fetching module {}{}/{}'.format(self.package, '@' + self.version if self.version else '', module))
		r = requests.get(URLUtilities.module(self.package, self.version, module))
		if r.status_code != 200:
			fatal('Module "{}/{}" not found'.format(self.package, module))
		html = self.save_html(r.text, self.documents_path(self.package, 'docs', urllib.parse.quote(module, '') + '.html'))
		self.cursor.execute(
			'INSERT OR IGNORE INTO searchIndex(name, type, path) VALUES (?,?,?);',
			[module, 'Module', os.path.join(self.package, 'docs', urllib.parse.quote(module, '') + '.html')])
		self.db.commit()
		return r.text

	@staticmethod
	def documents_path(*paths):
		return os.path.join(Generator.OUTPUT, 'Contents/Resources/Documents', *paths)

	def save_html(self, html, path):
		if self.package:
			prefix = r'../../'
		else:
			prefix = r''
		soup = BeautifulSoup(html, 'html.parser')
		# remove google font
		soup.find('link', href=re.compile(r'^https://fonts\.googleapis\.com/.*')).decompose()
		# remove widget script
		soup.find('script', src=re.compile(r'^https://pursuit\.purescript\.org/static/widget/.*')).decompose()
		# remove top banner
		soup.find('div', class_='top-banner').decompose()
		# remove searches
		for el in soup.find_all('a', href=re.compile(r'^/search\?.*')):
			el['href'] = '#'
		# replace version selector with actual version
		if self.version:
			select = soup.find('select', class_='version-selector')
			if select:
				dt = soup.new_tag('dt', text='Version', attrs={ 'class': 'grouped-list__title' })
				dd = soup.new_tag('dd', text=self.version, attrs={ 'class': 'grouped-list__item' })
				dl = select.find_next_sibling()
				dl.insert(0, dt)
				dl.insert(1, dd)
				select.decompose()
		# find anchors
		tlds = soup.find_all('div', class_='decl')
		for tld in tlds:
			self.process_decl(path, tld, soup)

		# enumerate elements
		for el in soup():
			for k, v in el.attrs.items():
				# collect assets
				if type(v) != str:
					continue
				urlprefix = URLUtilities.STATIC
				if v.startswith(urlprefix):
					v = v.split('?', 1)[0]
					self.assets.add(v)
					el.attrs[k] = prefix + 'static/' + v.split(urlprefix, 1)[1]
					continue
				# convert links
				urlprefix = 'https://pursuit.purescript.org/builtins/docs/'
				if v.startswith(urlprefix):
					docpath, hashtag, anchor = v.split(urlprefix, 1)[1].partition('#')
					el.attrs[k] = '{}builtins/docs/{}.html{}{}'.format(prefix, docpath, hashtag, anchor)
					continue
				if v.startswith('/packages/'):
					v = 'https://pursuit.purescript.org' + v
				urlprefix = 'https://pursuit.purescript.org/packages/'
				if v.startswith(urlprefix):
					docpath, hashtag, anchor = v.split(urlprefix, 1)[1].partition('#')
					segs = docpath.strip('/').split('/')
					if len(segs) == 4:
						el.attrs[k] = '{}{}/docs/{}.html{}{}'.format(prefix, segs[0], segs[3], hashtag, anchor)
					else:
						el.attrs[k] = '{}{}/docs/index.html{}{}'.format(prefix, segs[0], hashtag, anchor)
					continue
		with open(path, 'w') as f:
			f.write(str(soup))

	def process_decl(self, path, decl, soup):
		type_, name = decl.get('id').split(':', 1)
		type_ = self.convert_type(type_)
		name = unescape(name)
		signature = decl.find('pre', class_='decl__signature')
		if signature:
			if signature.code.find() == signature.code.find('span', class_='keyword', text='class'):
				type_ = 'Class'
		anchor_toc = '//apple_ref/cpp/{}/{}'.format(urllib.parse.quote(type_, ''), urllib.parse.quote(name, ''))
		self.cursor.execute(
			'INSERT OR IGNORE INTO searchIndex(name, type, path) VALUES (?,?,?);',
			[name, type_, '{}#{}'.format(os.path.relpath(path, self.documents_path()), anchor_toc)])
		a = soup.new_tag('a', attrs={ 'name': anchor_toc, 'class': 'dashAnchor' })
		decl.insert(0, a)
		if type_ == 'Class':
			members_lbl = decl.find('h4', text='Members')
			if members_lbl:
				for member in members_lbl.find_next_sibling().find_all('li', recursive=False):
					self.process_decl(path, member, soup)

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
			'v': 'Function',
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
