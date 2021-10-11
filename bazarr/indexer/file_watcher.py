# -*- coding: utf-8 -*-

import os
import logging
import warnings
from watchdog.events import PatternMatchingEventHandler

from config import settings
from bazarr.database import TableShowsRootfolder, TableMoviesRootfolder, TableShows, TableEpisodes, TableMovies
from indexer.utils import VIDEO_EXTENSION
from .series.local.series_indexer import get_series_match, get_series_metadata
from .series.local.episodes_indexer import get_episode_metadata
from .movies.local.movies_indexer import get_movies_match, get_movies_metadata
from list_subtitles import store_subtitles, store_subtitles_movie

# temporarily disable warning for mac users
warnings.simplefilter('ignore', category=UserWarning)
if settings.general.filewatcher_type == 'local':
    from watchdog_gevent import Observer
else:
    from watchdog.observers.polling import PollingObserverVFS as Observer
# then we enable it again just in case some other module would use this type of warnings
warnings.simplefilter('default', category=UserWarning)


class FileWatcher:
    def __init__(self):
        # make sure to only check for file with video extension
        self.patterns = [f"*{x}" for x in VIDEO_EXTENSION]
        self.ignore_patterns = None
        # ignore directory as it doesn't seems to work anyway... not with polling observer
        self.ignore_directories = True
        # enable case-sensitive monitoring to catch rename on other OS than Windows
        self.case_sensitive = True
        self.fs_event_handler = PatternMatchingEventHandler(self.patterns,
                                                            self.ignore_patterns,
                                                            self.ignore_directories,
                                                            self.case_sensitive)

        try:
            self.timeout = int(settings.general.filewatcher_timeout)
        except TypeError:
            self.timeout = 60
            logging.debug(f'BAZARR file watcher is using the default interval of {self.timeout} seconds.')
        else:
            logging.debug(f'BAZARR file watcher is using the configured interval of {self.timeout} seconds.')

        # start
        if settings.general.getboolean('use_series'):
            if settings.general.filewatcher_type == 'local':
                self.series_observer = Observer(timeout=self.timeout)
            else:
                self.series_observer = Observer(stat=os.stat, listdir=os.scandir, polling_interval=self.timeout)
            self.series_directories = None
        if settings.general.getboolean('use_movies'):
            if settings.general.filewatcher_type == 'local':
                self.movies_observer = Observer(timeout=self.timeout)
            else:
                self.movies_observer = Observer(stat=os.stat, listdir=os.scandir, polling_interval=self.timeout)
            self.movies_directories = None

        # get all root folders path
        self.all_paths = list(TableShowsRootfolder.select())
        self.all_paths.extend(list(TableMoviesRootfolder.select()))

    def on_any_event(self, event):
        # get the root folder containing the file
        root_dir = self.find_root_dir(event.src_path)
        if root_dir:
            if isinstance(root_dir, TableShowsRootfolder):
                if event.event_type == 'deleted':
                    # remove deleted episode
                    TableEpisodes.delete().where(TableEpisodes.path == event.src_path).execute()
                elif event.event_type in ['created', 'modified']:
                    # add or update existing episode
                    series_metadata = self.get_series_from_episode_path(event.src_path)
                    if not series_metadata:
                        # we can't find a series for this episode in database
                        if event.event_type == 'created':
                            # adding the first episode of a new series
                            series_dir = os.path.basename(os.path.dirname(os.path.dirname(event.src_path)))
                            # get matches from tmdb using the series directory name
                            series_matches = get_series_match(series_dir)
                            if series_matches:
                                # get series metadata for the first match
                                directory_metadata = get_series_metadata(series_matches[0]['tmdbId'], root_dir.rootId,
                                                                         series_dir)
                                if directory_metadata:
                                    try:
                                        # insert the series in database
                                        TableShows.insert(directory_metadata).execute()
                                    except Exception as e:
                                        logging.error(f'BAZARR is unable to insert this series to the database: '
                                                      f'"{directory_metadata["path"]}". The exception encountered is '
                                                      f'"{e}".')

                    if series_metadata:
                        # we found an existing series in database and will get the episode metadata from tmdb
                        episode_metadata = get_episode_metadata(event.src_path, series_metadata['tmdbId'],
                                                                series_metadata['seriesId'])
                        if episode_metadata:
                            if event.event_type == 'created':
                                # insert the new episode
                                TableEpisodes.insert(episode_metadata).execute()
                            else:
                                # or update the existing one
                                TableEpisodes.update(episode_metadata) \
                                    .where(TableEpisodes.path == event.src_path) \
                                    .execute()
                            # store the embedded and external subtitles for that episode in database
                            store_subtitles(event.src_path, use_cache=False)
                elif event.event_type in 'moved':
                    # get the series metadata using the episode source path
                    series_metadata = self.get_series_from_episode_path(event.src_path)
                    if series_metadata:
                        # get episode metadata using the destination path
                        episode_metadata = get_episode_metadata(event.dest_path, series_metadata['tmdbId'],
                                                                series_metadata['seriesId'])
                        if episode_metadata:
                            # update the episode in database and store subtitles in database
                            TableEpisodes.update(episode_metadata).where(TableEpisodes.path == event.src_path).execute()
                            store_subtitles(event.dest_path, use_cache=False)
            else:
                if event.event_type == 'deleted':
                    # remove deleted movie
                    TableMovies.delete().where(TableMovies.path == event.src_path).execute()
                elif event.event_type in ['created', 'modified']:
                    # add or update existing movie
                    try:
                        # get the movie metadata using the source path
                        movie_metadata = TableMovies.select().where(TableMovies.path == event.src_path).dicts().get()
                    except TableMovies.DoesNotExist:
                        # we can't find this movie in database
                        if event.event_type == 'created':
                            # adding the the movie
                            movie_dir = os.path.basename(os.path.dirname(event.src_path))
                            # get matches from tmdb using the movie directory name
                            movie_matches = get_movies_match(movie_dir)
                            if movie_matches:
                                # get movie metadata for the first match
                                directory_metadata = get_movies_metadata(movie_matches[0]['tmdbId'], root_dir.rootId,
                                                                         dir_name=movie_dir)
                                if directory_metadata:
                                    try:
                                        # insert the movie in database
                                        TableMovies.insert(directory_metadata).execute()
                                    except Exception as e:
                                        logging.error(f'BAZARR is unable to insert this movie to the database: '
                                                      f'"{directory_metadata["path"]}". The exception encountered is '
                                                      f'"{e}".')
                                    else:
                                        # store the embedded and external subtitles for that movie in database
                                        store_subtitles_movie(event.src_path, use_cache=False)

                    else:
                        # we found an existing movie in database and will get the movie metadata from tmdb
                        movie_metadata = get_movies_metadata(movie_metadata['tmdbId'], root_dir.rootId,
                                                             movie_path=event.src_path)
                        if movie_metadata:
                            # we update the existing movie
                            TableMovies.update(movie_metadata) \
                                .where(TableMovies.path == event.src_path) \
                                .execute()
                            # store the embedded and external subtitles for that movie in database
                            store_subtitles_movie(event.src_path, use_cache=False)
                elif event.event_type in 'moved':
                    try:
                        # get the movie metadata using the source path
                        movie_metadata = TableMovies.select().where(TableMovies.path == event.src_path).dicts().get()
                    except TableMovies.DoesNotExist:
                        # we can't find this movie in database
                        pass
                    else:
                        # we found an existing movie in database and will get the movie metadata from tmdb
                        movie_metadata = get_movies_metadata(movie_metadata['tmdbId'], root_dir.rootId,
                                                             movie_path=event.dest_path)
                        if movie_metadata:
                            # we update the existing movie
                            TableMovies.update(movie_metadata) \
                                .where(TableMovies.path == event.src_path) \
                                .execute()
                            # store the embedded and external subtitles for that movie in database
                            store_subtitles_movie(event.dest_path, use_cache=False)

    def find_root_dir(self, path):
        # return the parent root folder for that episode/movie path
        root_dir_list = [x for x in self.all_paths if x.path in path]
        if root_dir_list:
            return root_dir_list[0]

    @staticmethod
    def get_series_from_episode_path(path):
        # return series row for a specific episode path by moving up 2 level. ie: Series/Season/episode.mkv
        series_dir = os.path.dirname(os.path.dirname(path))
        try:
            series_metadata = TableShows.select().where(TableShows.path == series_dir).dicts().get()
        except TableShows.DoesNotExist:
            return None
        else:
            return series_metadata

    def config(self):
        # config the event handler and observers
        self.fs_event_handler.on_any_event = self.on_any_event

        # get all root folder paths and add them to the observers
        if settings.general.getboolean('use_series'):
            self.series_directories = [x['path'] for x in TableShowsRootfolder.select().dicts()]
            for series_directory in self.series_directories:
                if os.path.exists(series_directory):
                    self.series_observer.schedule(self.fs_event_handler, series_directory, recursive=True)
                    logging.debug(f'BAZARR is watching this series root directory: {series_directory}')
                else:
                    logging.error(f'BAZARR cannot access this series root directory: {series_directory}')
        if settings.general.getboolean('use_movies'):
            self.movies_directories = [x['path'] for x in TableMoviesRootfolder.select().dicts()]
            for movies_directory in self.movies_directories:
                if os.path.exists(movies_directory):
                    self.movies_observer.schedule(self.fs_event_handler, movies_directory, recursive=True)
                    logging.debug(f'BAZARR is watching this movies root directory: {movies_directory}')
                else:
                    logging.error(f'BAZARR cannot access this movies root directory: {movies_directory}')

    def start(self):
        logging.info('BAZARR is starting file system watchers...')
        if settings.general.getboolean('use_series'):
            self.series_observer.start()
        if settings.general.getboolean('use_movies'):
            self.movies_observer.start()
        self.config()
        logging.info('BAZARR is watching for file system changes.')


fileWatcher = FileWatcher()
