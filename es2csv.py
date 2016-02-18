#!/usr/bin/env python
"""
title:           A CLI tool for exporting data from Elasticsearch into a CSV file.
description:     Command line utility, written in Python, for querying Elasticsearch in Lucene query syntax and for exporting documents into a CSV file.
usage:           es2csv -q '*' -i _all -o ~/file.csv -k -m 100
                 es2csv -q '*' -i logstash-2015-01-* -f host status message -o ~/file.csv
                 es2csv -q 'host: localhost' -i logstash-2015-01-01 logstash-2015-01-02 -f host status message -o ~/file.csv
                 es2csv -q 'host: localhost AND status: GET' -u http://kibana.com:80/es/ -o ~/file.csv
                 es2csv -q '*' -t dev prod -u http://login:password@kibana.com:6666/es/ -o ~/file.csv
"""
import os
import sys
import time
import argparse
import json
import csv
import elasticsearch
import progressbar
from functools import wraps

FLUSH_BUFFER = 1000  # Chunk of docs to flush in temp file
CONNECTION_TIMEOUT = 120
TIMES_TO_TRY = 3
RETRY_DELAY = 60
__version__ = '1.0.1'


# Retry decorator for functions with exceptions
def retry(ExceptionToCheck, tries=TIMES_TO_TRY, delay=RETRY_DELAY):
    def deco_retry(f):
        @wraps(f)
        def f_retry(*args, **kwargs):
            mtries = tries
            while mtries > 0:
                try:
                    return f(*args, **kwargs)
                except ExceptionToCheck as e:
                    print(e)
                    print('Retrying in %d seconds ...' % delay)
                    time.sleep(delay)
                    mtries -= 1
                else:
                    print('Done.')
            try:
                return f(*args, **kwargs)
            except ExceptionToCheck as e:
                print(e)
                exit(1)

        return f_retry

    return deco_retry


class Es2csv:

    def __init__(self, opts):
        self.opts = opts

        self.num_results = 0
        self.scroll_ids = []
        self.scroll_size = 100
        self.scroll_time = '30m'

        self.csv_headers = []
        self.tmp_file = '%s.tmp' % opts.output_file

    @retry(elasticsearch.exceptions.ConnectionError, tries=TIMES_TO_TRY)
    def create_connection(self):
        es = elasticsearch.Elasticsearch(self.opts.url, timeout=CONNECTION_TIMEOUT)
        es.cluster.health()
        self.es_conn = es

    @retry(elasticsearch.exceptions.ConnectionError, tries=TIMES_TO_TRY)
    def check_indexes(self):
        indexes = self.opts.index_prefixes
        if '_all' in indexes:
            indexes = ['_all']
        else:
            indexes = [index for index in indexes if self.es_conn.indices.exists(index)]
            if not indexes:
                print('Any of index(es) %s does not exist in %s.' % (', '.join(self.opts.index_prefixes), self.opts.url))
                exit(1)
        self.opts.index_prefixes = indexes

    @retry(elasticsearch.exceptions.ConnectionError, tries=TIMES_TO_TRY)
    def search_query(self):
        query = self.opts.query if not self.opts.tags else '%s AND tags:%s' % (
            self.opts.query, '(%s)' % ' AND '.join(self.opts.tags))

        if self.opts.debug_mode:
            print('Using these indices: %s' % ', '.join(self.opts.index_prefixes))
            print('Query: %s' % query)
            print('Output field(s): %s' % ', '.join(self.opts.fields))

        search_args = dict(
            index=','.join(self.opts.index_prefixes),
            q=query,
            search_type='scan',
            scroll=self.scroll_time,
            size=self.scroll_size,
            ignore_unavailable=self.opts.ignore_unavailable
        )

        if '_all' not in self.opts.fields:
            search_args['fields'] = ','.join(self.opts.fields)

        res = self.es_conn.search(**search_args)

        self.scroll_ids.append(res['_scroll_id'])
        self.num_results = res['hits']['total']

        print('Found %s results' % self.num_results)
        if self.opts.debug_mode:
            print(res)

        if self.num_results > 0:
            open(self.opts.output_file, 'w').close()
            open(self.tmp_file, 'w').close()

            hit_list = []
            total_lines = 0

            widgets = ['Run query ',
                       progressbar.Bar(left='[', marker='#', right=']'),
                       progressbar.FormatLabel(' [%(value)i/%(max)i] ['),
                       progressbar.Percentage(),
                       progressbar.FormatLabel('] [%(elapsed)s] ['),
                       progressbar.ETA(), '] [',
                       progressbar.FileTransferSpeed('docs'), ']'
                       ]
            bar = progressbar.ProgressBar(widgets=widgets, maxval=self.num_results).start()

            while total_lines != self.num_results:
                res = self.es_conn.scroll(scroll=self.scroll_time, scroll_id=res['_scroll_id'])
                if res['_scroll_id'] not in self.scroll_ids:
                    self.scroll_ids.append(res['_scroll_id'])

                for hit in res['hits']['hits']:
                    total_lines += 1
                    bar.update(total_lines)
                    hit_list.append(hit)
                    if len(hit_list) == FLUSH_BUFFER:
                        self.flush_to_file(hit_list)
                        hit_list = []
                    if self.opts.max_results:
                        if total_lines == self.opts.max_results:
                            self.flush_to_file(hit_list)
                            print('Hit max result limit: %s records' % self.opts.max_results)
                            return
            self.flush_to_file(hit_list)
            bar.finish()

    def flush_to_file(self, hit_list):
        def to_keyvalue_pairs(source, ancestors=[], header_delimeter='.'):
            def is_list(arg):
                return type(arg) is list

            def is_dict(arg):
                return type(arg) is dict

            if is_dict(source):
                for key in source.keys():
                    to_keyvalue_pairs(source[key], ancestors + [key])

            elif is_list(source):
                if self.opts.kibana_nested:
                    [to_keyvalue_pairs(item, ancestors) for item in source]
                else:
                    [to_keyvalue_pairs(item, ancestors + [str(index)]) for index, item in enumerate(source)]
            else:
                header = header_delimeter.join(ancestors)
                if header not in self.csv_headers:
                    self.csv_headers.append(header)
                try:
                    out[header] = '%s%s%s' % (out[header], self.opts.delimiter, source)
                except:
                    out[header] = source

        with open(self.tmp_file, 'a') as tmp_file:
            for hit in hit_list:
                out = {}
                if '_source' in hit:
                    to_keyvalue_pairs(hit['_source'])
                    tmp_file.write('%s\n' % json.dumps(out))
                elif 'fields' in hit:
                    to_keyvalue_pairs(hit['fields'])
                    tmp_file.write('%s\n' % json.dumps(out))
        tmp_file.close()

    def write_to_csv(self):
        if self.num_results > 0:
            self.num_results = sum(1 for line in open(self.tmp_file, 'r'))
            if self.num_results > 0:
                self.csv_headers.sort()
                output_file = open(self.opts.output_file, 'a')
                csv_writer = csv.DictWriter(output_file, fieldnames=self.csv_headers, delimiter=self.opts.delimiter)
                csv_writer.writeheader()
                timer = 0
                widgets = ['Write to csv ',
                           progressbar.Bar(left='[', marker='#', right=']'),
                           progressbar.FormatLabel(' [%(value)i/%(max)i] ['),
                           progressbar.Percentage(),
                           progressbar.FormatLabel('] [%(elapsed)s] ['),
                           progressbar.ETA(), '] [',
                           progressbar.FileTransferSpeed('lines'), ']'
                           ]
                bar = progressbar.ProgressBar(widgets=widgets, maxval=self.num_results).start()

                for line in open(self.tmp_file, 'r'):
                    timer += 1
                    bar.update(timer)
                    line_as_dict = json.loads(line)
                    line_dict_utf8 = {k: v.encode('utf8') if isinstance(v, unicode) else v for k, v in line_as_dict.items()}
                    csv_writer.writerow(line_dict_utf8)
                output_file.close()
                bar.finish()
            else:
                print('There is no docs with selected field(s): %s.' % ','.join(self.opts.fields))
            os.remove(self.tmp_file)

    def clean_scroll_ids(self):
        try:
            self.es_conn.clear_scroll(body=','.join(self.scroll_ids))
        except:
            pass


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('-q', '--query', dest='query', type=str, required=True, help='Query string in Lucene syntax.')
    p.add_argument('-u', '--url', dest='url', default='http://localhost:9200', type=str, help='Elasticsearch host URL. Default is %(default)s.')
    p.add_argument('-i', '--index-prefixes', dest='index_prefixes', default=['logstash-*'], type=str, nargs='+', metavar='INDEX', help='Index name prefix(es). Default is %(default)s.')
    p.add_argument('-t', '--tags', dest='tags', type=str, nargs='+', help='Query tags.')
    p.add_argument('-o', '--output_file', dest='output_file', type=str, required=True, metavar='FILE', help='CSV file location.')
    p.add_argument('-f', '--fields', dest='fields', default=['_all'], type=str, nargs='+', help='List of selected fields in output. Default is %(default)s.')
    p.add_argument('-d', '--delimiter', dest='delimiter', default=',', type=str, help='Delimiter to use in CSV file. Default is "%(default)s".')
    p.add_argument('-m', '--max', dest='max_results', default=0, type=int, metavar='INTEGER', help='Maximum number of results to return. Default is %(default)s.')
    p.add_argument('-k', '--kibana_nested', dest='kibana_nested', action='store_true', help='Format nested fields in Kibana style.')
    p.add_argument('-c', '--ignore-unavailable', dest='ignore_unavailable', action='store_true', help='Ignore unavailable indexes (missing or closed).')
    p.add_argument('-v', '--version', action='version', version='%(prog)s ' + __version__, help='Show version and exit.')
    p.add_argument('--debug', dest='debug_mode', action='store_true', help='Debug mode on.')

    if len(sys.argv) == 1:
        p.print_help()
        exit()

    opts = p.parse_args()
    es = Es2csv(opts)
    es.create_connection()
    es.check_indexes()
    es.search_query()
    es.write_to_csv()
    es.clean_scroll_ids()

if __name__ == '__main__':
    main()
