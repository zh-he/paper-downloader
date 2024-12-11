import logging
import multiprocessing as mp
import os
import random
import re
import time
from abc import ABC, abstractmethod
from enum import Enum
from typing import Dict, List, Tuple

import downloader
import html_parser
import utils
from tqdm import tqdm

_Tag = html_parser.Tag


class DBLPVenueType(Enum):
    CONFERENCE = 'conf'
    JOURNAL = 'journals'


##################################################################
#                       Abstrace Class                           #
##################################################################

class Base(ABC):
    def __init__(self,
                 save_dir: str,
                 sleep_time_per_paper: float,
                 parallel: bool = False,
                 proxies: Dict[str, str] = None,
                 **kwargs):
        self.save_dir = save_dir
        self.sleep_time_per_paper = sleep_time_per_paper
        self.parallel = parallel
        self.proxies = proxies

        if 'venue_name' in kwargs:
            self.venue_name = kwargs['venue_name']
        else:
            self.venue_name = None

        if 'test_mode' in kwargs:
            self.test_mode = kwargs['test_mode']
        else:
            self.test_mode = False

        self.url = self._get_url()
        if not self.url:
            return

        self.dblp_url_prefix = 'https://dblp.org/db/'

    def process(self) -> None:
        if not os.path.exists(self.save_dir):
            os.makedirs(self.save_dir)

        paper_list = self._get_paper_list()
        if not paper_list:
            logging.error('The paper list is empty!')
            return None

        if self.test_mode:
            test_paper = random.sample(paper_list, 1)[0]
            self._process_one(test_paper)
        else:
            if self.parallel:
                with mp.Pool(processes=mp.cpu_count()) as pool:
                    with tqdm(total=len(paper_list)) as progress_bar:
                        for _ in pool.imap_unordered(self._process_one, paper_list):
                            progress_bar.update(1)
            else:
                for paper_entry in tqdm(paper_list):
                    self._process_one(paper_entry)

    def _process_one(self, paper_info: Tuple[str, str]):
        paper_title, paper_url = paper_info

        pid = os.getpid()
        if self._paper_url_is_file_url(paper_url):
            logging.info(f'(pid {pid}) downloading paper: {paper_url}')
            self._download_paper(paper_url, paper_title)
        else:
            logging.info(f'(pid {pid}) downloading html: {paper_url}')
            paper_html = downloader.download_html(paper_url, proxies=self.proxies)
            if paper_html is None:
                return None

            paper_file_url = self._get_paper_file_url(paper_html)
            if paper_file_url is None:
                return None
            logging.info(f'(pid {pid}) downloading paper: {paper_file_url}')
            self._download_paper(utils.get_absolute_url(paper_url, paper_file_url), paper_title)

            paper_slides_url = self._get_slides_file_url(paper_html)
            if paper_slides_url is None:
                return None
            logging.info(f'(pid {pid}) downloading slides: {paper_slides_url}')
            self._download_slides(utils.get_absolute_url(paper_url, paper_slides_url), paper_title)

        if self.sleep_time_per_paper:
            time.sleep(self.sleep_time_per_paper)

    @staticmethod
    def _paper_url_is_file_url(paper_url: str) -> bool:
        file_ext_name = '.pdf'
        if paper_url.lower().endswith(file_ext_name):
            return True

        paper_url = downloader.get_real_url(paper_url)
        if paper_url.lower().endswith(file_ext_name):
            return True

        return False

    def _get_paper_list(self) -> List[Tuple[str, str]] | None:
        logging.info(f'downloading {self.url}')
        paper_list_html = downloader.download_html(self.url, proxies=self.proxies)
        if not paper_list_html or not paper_list_html.strip():
            return None

        if self.url.startswith(self.dblp_url_prefix):
            return self._get_paper_list_by_dblp(paper_list_html)

        return self._get_paper_list_by_diy(paper_list_html)

    def _get_paper_list_by_diy(self, html) -> List[Tuple[str, str]] | None:
        result_tuple = self._get_paper_title_and_url_list_by_diy(html)
        if not result_tuple:
            logging.error(f'Unable to extract title and URL from the given URL ({self.url}).')
            return None

        paper_title_list, paper_url_list = result_tuple
        num_titles = len(paper_title_list)
        num_urls = len(paper_url_list)
        if num_titles != num_urls:
            logging.error(f'Number of titles ({num_titles}) is not equal to number of urls ({num_urls}).')
            return None

        paper_list = []
        for paper_no in range(num_titles):
            paper_title = html_parser.get_text(paper_title_list[paper_no])
            if not paper_title:
                continue

            paper_url = html_parser.get_href(paper_url_list[paper_no])
            if not paper_url:
                continue

            paper_list.append((paper_title, utils.get_absolute_url(self.url, paper_url)))
        return paper_list

    def _get_paper_list_by_dblp(self, html) -> List[Tuple[str, str]] | None:
        paper_list = []

        logging.info(f'parsing html!')
        parser = html_parser.get_parser(html)

        if self._get_dblp_venue_type() == DBLPVenueType.CONFERENCE.value:
            paper_list_selector = '.inproceedings'
        else:
            paper_list_selector = '.article'

        paper_entry_list = parser.select(paper_list_selector)
        logging.info(f'number of papers: {len(paper_entry_list)}')

        for paper_entry in paper_entry_list:
            paper_title = paper_entry.select('.title')

            if not paper_title:
                continue

            paper_title = paper_title[0].text.strip()
            paper_url = html_parser.get_href_first(paper_entry.select('.drop-down:first-child a'))

            paper_list.append((paper_title, paper_url))

        return paper_list

    def _get_dblp_venue_type(self) -> str | None:
        start = self.url.find(self.dblp_url_prefix)
        if start == -1:
            return None

        start += len(self.dblp_url_prefix)
        end = self.url[start:].find('/')
        if end == -1:
            return None
        return self.url[start: start + end]

    def _get_filename(self, paper_title: str, paper_url: str, name_suffix: str = None) -> str:
        paper_title = re.sub('[/.]+', '', paper_title)
        paper_title = re.sub('\W+', '-', paper_title)

        paper_pathname = os.path.join(self.save_dir, paper_title)
        if name_suffix:
            paper_pathname += '-' + name_suffix

        paper_ext_name = utils.get_file_extension_name_or_default(paper_url, default_value='.pdf')
        return paper_pathname + paper_ext_name

    def _download_paper(self, paper_file_url: str, paper_title: str) -> None:
        if not paper_file_url:
            return None

        paper_filename = self._get_filename(paper_title, paper_file_url, name_suffix='Paper')
        if not os.path.exists(paper_filename):
            downloader.download_file(paper_file_url, paper_filename, proxies=self.proxies)

    def _download_slides(self, paper_slides_url: str, paper_title: str) -> None:
        if not paper_slides_url:
            return None

        slides_filename = self._get_filename(paper_title, paper_slides_url, name_suffix='Slides')
        if not os.path.exists(slides_filename):
            downloader.download_file(paper_slides_url, slides_filename, proxies=self.proxies)

    @abstractmethod
    def _get_url(self) -> str | None:
        pass

    @abstractmethod
    def _get_paper_title_and_url_list_by_diy(self, html) -> Tuple[List[_Tag], List[_Tag]] | None:
        pass

    @abstractmethod
    def _get_paper_file_url(self, html: str) -> str:
        pass

    @abstractmethod
    def _get_slides_file_url(self, html: str) -> str:
        pass


class Conference(Base):

    def __init__(self, save_dir: str, sleep_time_per_paper: float, **kwargs):
        if 'year' in kwargs:
            self.year = kwargs['year']
        else:
            self.year = None

        super().__init__(save_dir, sleep_time_per_paper, **kwargs)

    @abstractmethod
    def _get_url(self) -> str | None:
        pass

    @abstractmethod
    def _get_paper_title_and_url_list_by_diy(self, html) -> Tuple[List[_Tag], List[_Tag]] | None:
        pass

    @abstractmethod
    def _get_paper_file_url(self, html: str) -> str:
        pass

    @abstractmethod
    def _get_slides_file_url(self, html: str) -> str:
        pass


class Journal(Base):

    def __init__(self, save_dir: str, sleep_time_per_paper: float, **kwargs):
        if 'volume' in kwargs:
            self.volume = kwargs['volume']
        else:
            self.volume = None

        super().__init__(save_dir, sleep_time_per_paper, **kwargs)

    @abstractmethod
    def _get_url(self) -> str | None:
        pass

    @abstractmethod
    def _get_paper_title_and_url_list_by_diy(self, html) -> Tuple[List[_Tag], List[_Tag]] | None:
        pass

    @abstractmethod
    def _get_paper_file_url(self, html: str) -> str:
        pass

    @abstractmethod
    def _get_slides_file_url(self, html: str) -> str:
        pass


##################################################################
#                           Conference                           #
##################################################################

class USENIX(Conference):

    def _get_url(self) -> str | None:
        if self.venue_name == 'atc':
            self.venue_name = 'usenix'

        available_confs = ['fast', 'osdi', 'usenix', 'nsdi', 'uss']
        if self.venue_name not in available_confs:
            logging.error(f'error: unknown confernce {self.venue_name}')
            return None

        return f'https://dblp.org/db/conf/{self.venue_name}/{self.venue_name}{self.year}.html'

    def _get_paper_title_and_url_list_by_diy(self, html) -> Tuple[List[_Tag], List[_Tag]] | None:
        pass

    def _get_paper_file_url(self, html: str) -> str:
        result = html_parser.parse_href(html, '.file a')
        if result:
            return result

        return html_parser.parse_href(html, '[href$=".pdf"]')

    def _get_slides_file_url(self, html: str) -> str:
        return html_parser.parse_href(html, '.usenix-schedule-slides a')


class NDSS(Conference):

    def _get_url(self) -> str | None:
        return f'https://dblp.org/db/conf/ndss/ndss{self.year}.html'

    def _get_paper_title_and_url_list_by_diy(self, html) -> Tuple[List[_Tag], List[_Tag]] | None:
        pass

    def _get_paper_file_url(self, html: str) -> str:
        return html_parser.parse_href(html, '.pdf-button')

    def _get_slides_file_url(self, html: str) -> str:
        return html_parser.parse_href(html, '.button-slides')


class AAAI(Conference):
    def _get_url(self) -> str | None:
        return f'https://dblp.org/db/conf/aaai/aaai{self.year}.html'

    def _get_paper_title_and_url_list_by_diy(self, html) -> Tuple[List[_Tag], List[_Tag]] | None:
        pass

    def _get_paper_file_url(self, html: str) -> str:
        return html_parser.parse_href(html, '.pdf')

    def _get_slides_file_url(self, html: str) -> str:
        pass


class IJCAI(Conference):
    def _get_url(self) -> str | None:
        return f'https://dblp.org/db/conf/ijcai/ijcai{self.year}.html'

    def _get_paper_title_and_url_list_by_diy(self, html) -> Tuple[List[_Tag], List[_Tag]] | None:
        pass

    def _get_paper_file_url(self, html: str) -> str:
        return html_parser.parse_href(html, '.btn-download:first-child')

    def _get_slides_file_url(self, html: str) -> str:
        pass


class CVF(Conference):
    def _get_url(self) -> str | None:
        available_confs = ['CVPR', 'ICCV']
        venue_name = self.venue_name.upper()
        if venue_name not in available_confs:
            logging.error(f'error: unknown conference {venue_name}')
            return None

        return f'https://openaccess.thecvf.com/{venue_name}{self.year}?day=all'

    def _get_paper_title_and_url_list_by_diy(self, html) -> Tuple[List[_Tag], List[_Tag]] | None:
        parser = html_parser.get_parser(html)

        paper_title_list = parser.select('.ptitle a')
        paper_url_list = parser.select('.ptitle + dd + dd > a:first-child')

        return paper_title_list, paper_url_list

    def _get_paper_file_url(self, html: str) -> str:
        pass

    def _get_slides_file_url(self, html: str) -> str:
        pass


class ECCV(Conference):
    def _get_url(self) -> str | None:
        return 'https://www.ecva.net/papers.php'

    def _get_paper_title_and_url_list_by_diy(self, html) -> Tuple[List[_Tag], List[_Tag]] | None:
        start_year = 2018
        if self.year < start_year:
            logging.error(f'{self.__class__.__name__}: Unsupported year: {self.year}, must be [{start_year}, Now]')
            return None

        parser = html_parser.get_parser(html)

        year_idx = -1
        year_tag_list = parser.select('button.accordion')
        for idx, year_tag in enumerate(year_tag_list):
            year = re.search(r'\b(\d{4})\b', year_tag.text.strip())
            if year is None:
                continue

            if int(year.group(1)) == self.year:
                year_idx = idx
                break

        if year_idx == -1:
            return None

        nth_year_paper_list = parser.select('#content')[year_idx]
        paper_title_list = nth_year_paper_list.select('.ptitle a')
        paper_url_list = nth_year_paper_list.select('.ptitle + dd + dd > a')

        return paper_title_list, paper_url_list

    def _get_paper_file_url(self, html: str) -> str:
        pass

    def _get_slides_file_url(self, html: str) -> str:
        pass


class ICLR(Conference):

    def _get_url(self) -> str | None:
        return f'https://dblp.org/db/conf/iclr/iclr{self.year}.html'

    def _get_paper_title_and_url_list_by_diy(self, html) -> Tuple[List[_Tag], List[_Tag]] | None:
        pass

    def _get_paper_file_url(self, html: str) -> str:
        # arXiv.org
        if self.year <= 2016:
            return html_parser.parse_href(html, '.download-pdf')

        # openreview.net
        return html_parser.parse_href(html, 'a[href^="/pdf"]')

    def _get_slides_file_url(self, html: str) -> str:
        pass


class ICML(Conference):

    def _get_url(self) -> str | None:
        return f'https://dblp.org/db/conf/icml/icml{self.year}.html'

    def _get_paper_title_and_url_list_by_diy(self, html) -> Tuple[List[_Tag], List[_Tag]] | None:
        pass

    def _get_paper_file_url(self, html: str) -> str:
        # ACM
        if self.year < 2010:
            return ""

        # mlr.press
        if 2010 <= self.year <= 2023:
            return html_parser.parse_href(html, 'a[href$=".pdf"]')

        # openreview.net
        return html_parser.parse_href(html, 'a[href^="/pdf"]')

    def _get_slides_file_url(self, html: str) -> str:
        pass


class NeurIPS(Conference):

    def _get_url(self) -> str | None:
        if self.year <= 2019:
            venue_name = 'nips'
        else:
            venue_name = 'neurips'

        return f'https://dblp.org/db/conf/nips/{venue_name}{self.year}.html'

    def _get_paper_title_and_url_list_by_diy(self, html) -> Tuple[List[_Tag], List[_Tag]] | None:
        pass

    def _get_paper_file_url(self, html: str) -> str:
        return html_parser.parse_href(html, '.col a.btn-primary')

    def _get_slides_file_url(self, html: str) -> str:
        pass


class ACL(Conference):

    def _get_url(self) -> str | None:
        available_confs = ['acl', 'emnlp', 'naacl']

        venue_name = self.venue_name
        if venue_name not in available_confs:
            logging.error(f'error: unknown conference {venue_name}')
            return None

        if ((venue_name == 'acl' and self.year >= 2012)
                or (venue_name == 'emnlp' and 2019 <= self.year <= 2021)
                or (venue_name == 'naacl' and 2018 <= self.year <= 2019)):
            suffix = '-1'
        else:
            suffix = ''

        return f'https://dblp.org/db/conf/{venue_name}/{venue_name}{self.year}{suffix}.html'

    def _get_paper_title_and_url_list_by_diy(self, html) -> Tuple[List[_Tag], List[_Tag]] | None:
        pass

    def _get_paper_file_url(self, html: str) -> str:
        return html_parser.parse_href(html, '.acl-paper-link-block .btn-primary')

    def _get_slides_file_url(self, html: str) -> str:
        pass


class RSS(Conference):

    def _get_url(self) -> str | None:
        return f'https://dblp.org/db/conf/rss/rss{self.year}.html'

    def _get_paper_title_and_url_list_by_diy(self, html) -> Tuple[List[_Tag], List[_Tag]] | None:
        pass

    def _get_paper_file_url(self, html: str) -> str:
        pass

    def _get_slides_file_url(self, html: str) -> str:
        pass


##################################################################
#                           Journal                              #
##################################################################

class PVLDB(Journal):

    def _get_url(self) -> str | None:
        return f'https://dblp.org/db/journals/pvldb/pvldb{self.volume}.html'

    def _get_paper_title_and_url_list_by_diy(self, html) -> Tuple[List[_Tag], List[_Tag]] | None:
        pass

    def _get_paper_file_url(self, html: str) -> str:
        pass

    def _get_slides_file_url(self, html: str) -> str:
        pass


class JMLR(Journal):

    def _get_url(self) -> str | None:
        return f'https://jmlr.org/papers/v{self.volume}/'

    def _get_paper_title_and_url_list_by_diy(self, html) -> Tuple[List[_Tag], List[_Tag]] | None:
        parser = html_parser.get_parser(html)
        paper_title_list = parser.select('dl dt')
        paper_url_list = parser.select('a[href$=".pdf"][target="_blank"]')

        return paper_title_list, paper_url_list

    def _get_paper_file_url(self, html: str) -> str:
        pass

    def _get_slides_file_url(self, html: str) -> str:
        pass


__venue_dict = {
    # Conference
    # Operating System/Storage System
    'fast': USENIX,
    'osdi': USENIX,
    'atc': USENIX,

    # Computer Networks
    'nsdi': USENIX,

    # Computer security
    'uss': USENIX,
    'ndss': NDSS,

    # Artificial Intelligence
    'aaai': AAAI,
    'ijcai': IJCAI,

    # Computer Vision
    'cvpr': CVF,
    'iccv': CVF,
    'eccv': ECCV,

    # Machine Learning
    'iclr': ICLR,
    'icml': ICML,
    'neurips': NeurIPS,
    # alias for 'neurips'
    'nips': NeurIPS,

    # Natural Language Processing
    'acl': ACL,
    'emnlp': ACL,
    'naacl': ACL,

    # Robotics
    'rss': RSS,

    # Journal
    # Databases
    'pvldb': PVLDB,
    # alias for 'pvldb'
    'vldb': PVLDB,

    'jmlr': JMLR,
}


def get_available_venues() -> str:
    return ','.join(__venue_dict.keys())


def parse_venue(venue: str) -> Base | None:
    venue = venue.lower()
    if venue not in __venue_dict.keys():
        return None
    return __venue_dict[venue]


def is_conference(venue_publisher: type):
    return issubclass(venue_publisher, Conference)


def is_journal(venue_publisher: type):
    return issubclass(venue_publisher, Journal)
