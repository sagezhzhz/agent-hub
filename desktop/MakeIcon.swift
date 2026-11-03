import AppKit

let destination = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
for size in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let pixels = size * scale
        let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels,
            bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
            colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        let context = NSGraphicsContext(bitmapImageRep: bitmap)!
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = context
        context.cgContext.scaleBy(x: CGFloat(pixels) / 1024, y: CGFloat(pixels) / 1024)
        let tile = NSBezierPath(roundedRect: NSRect(x: 52, y: 52, width: 920, height: 920), xRadius: 210, yRadius: 210)
        NSColor(red: 0.065, green: 0.12, blue: 0.12, alpha: 1).setFill(); tile.fill()
        let points = [NSPoint(x: 300, y: 724), NSPoint(x: 724, y: 724),
                      NSPoint(x: 300, y: 300), NSPoint(x: 724, y: 300)]
        let link = NSBezierPath(); link.lineWidth = 32; link.lineCapStyle = .round
        for point in points { link.move(to: NSPoint(x: 512, y: 512)); link.line(to: point) }
        NSColor(red: 0.33, green: 0.49, blue: 0.45, alpha: 1).setStroke(); link.stroke()
        for (index, point) in points.enumerated() {
            let colors = [NSColor(red: 0.45, green: 0.85, blue: 0.68, alpha: 1),
                          NSColor(red: 0.97, green: 0.74, blue: 0.43, alpha: 1),
                          NSColor(red: 0.42, green: 0.64, blue: 0.95, alpha: 1),
                          NSColor(red: 0.75, green: 0.64, blue: 0.91, alpha: 1)]
            colors[index].setFill()
            NSBezierPath(roundedRect: NSRect(x: point.x - 83, y: point.y - 83, width: 166, height: 166),
                         xRadius: 49, yRadius: 49).fill()
        }
        NSColor(red: 0.95, green: 0.96, blue: 0.89, alpha: 1).setFill()
        NSBezierPath(roundedRect: NSRect(x: 404, y: 404, width: 216, height: 216), xRadius: 66, yRadius: 66).fill()
        NSGraphicsContext.restoreGraphicsState()
        let suffix = scale == 2 ? "@2x" : ""
        try bitmap.representation(using: .png, properties: [:])!
            .write(to: destination.appendingPathComponent("icon_\(size)x\(size)\(suffix).png"))
    }
}
