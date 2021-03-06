girder.wrap(girder.views.ItemListWidget, 'render', function (render) {
    render.call(this);
    girder.views.largeImageConfig.getSettings(_.bind(function (settings) {
        if (settings['large_image.show_thumbnails'] === false ||
                $('.large_image_thumbnail', this.$el).length > 0) {
            return this;
        }
        var items = this.collection.toArray();
        var parent = this.$el;
        var hasAnyLargeImage = _.some(items, function (item) {
            return item.has('largeImage');
        });
        if (hasAnyLargeImage) {
            $.each(items, function (idx, item) {
                var elem = $('<div class="large_image_thumbnail"/>');
                if (item.get('largeImage')) {
                    elem.append($('<img/>').attr(
                            'src', girder.apiRoot + '/item/' + item.id +
                            '/tiles/thumbnail?width=160&height=100'));
                    $('img', elem).one('error', function () {
                        $('img', elem).addClass('failed-to-load');
                    });
                }
                $('a[g-item-cid="' + item.cid + '"]>i', parent).before(elem);
            });
        }
        return this;
    }, this));
});
