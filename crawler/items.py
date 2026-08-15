import scrapy


class ProcurementNoticeItem(scrapy.Item):
    """One notice, in the shape the ``tenders`` table stores it.

    Shared by every spider: the World Bank and ADB feeds describe procurement
    very differently, but each spider's parser maps its feed onto this one
    vocabulary so that scoring, classification and the dashboard never have to
    branch on where a notice came from.
    """

    notice_id = scrapy.Field()
    source = scrapy.Field()
    notice_type = scrapy.Field()
    noticedate = scrapy.Field()
    notice_status = scrapy.Field()
    submission_deadline = scrapy.Field()
    project_id = scrapy.Field()
    project_name = scrapy.Field()
    project_country = scrapy.Field()
    bid_reference_no = scrapy.Field()
    bid_description = scrapy.Field()
    procurement_group = scrapy.Field()
    procurement_method_code = scrapy.Field()
    procurement_method_name = scrapy.Field()
    sector = scrapy.Field()
    contact_organization = scrapy.Field()
    contact_name = scrapy.Field()
    contact_email = scrapy.Field()
    contact_phone = scrapy.Field()
    contact_address = scrapy.Field()
    notice_text = scrapy.Field()
    notice_text_clean = scrapy.Field()
    parsed_fields = scrapy.Field()
    notice_url = scrapy.Field()
    content_hash = scrapy.Field()
    dedup_key = scrapy.Field()
    # True when the spider knows this notice has a body it could not fetch, as
    # opposed to one it has no body at all. Not a column: it tells the pipeline
    # that the blanks in this item are missing rather than empty, so an update
    # keeps what is already stored instead of erasing it.
    partial = scrapy.Field()
